"""Non-destructive live playlists: source videos and chat clocks stay separate.

The first indexed segment represents the collection in the library. Membership
and task projections are durable; no concat, transcoding or chat rewriting runs.
"""
from datetime import datetime
import uuid

from . import db

SCHEMA = """
ALTER TABLE live_recordings ADD COLUMN is_collection INTEGER NOT NULL DEFAULT 0;
CREATE INDEX live_collection_entry ON live_recordings(entry_id) WHERE is_collection=1;
CREATE INDEX live_collection_sources ON live_recordings(sub_id,session_id,started_at) WHERE is_aggregate=0;
"""
TERMINAL = ('completed', 'stopped', 'interrupted')


def _seconds(start, end):
    try:
        return max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except (TypeError, ValueError):
        return None


def parts(connection, identity):
    rows = connection.execute('''SELECT r.*,e.root_id,e.rel_dir,e.availability FROM live_recordings r
        LEFT JOIN media_entries e ON e.id=r.entry_id
        WHERE r.merged_into=? AND r.is_aggregate=0 ORDER BY r.started_at,r.id''', (identity,))
    result, previous = [], None
    for row in rows:
        item = dict(row)
        # finished_at is finalization completion, not an invented video end.
        item['gap_before_seconds'] = _seconds(previous['finished_at'], item['started_at']) if previous else None
        item['gap_basis'] = 'previous_finalization_to_next_start'
        result.append(item)
        previous = item
    return result


def refresh():
    """Atomically replace legacy daily projections with session collections.

    Physical daily derivatives remain on disk and in history. Recover originals
    before regrouping so a legacy day containing different sessions can split
    into separate collections (or a standalone recording) without losing entries.
    """
    changed = set()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        legacy = c.execute('''SELECT * FROM live_recordings
            WHERE is_aggregate=1 AND is_collection=0 AND merged_into IS NULL''').fetchall()
        for old in legacy:
            originals = c.execute('''SELECT * FROM live_recordings
                WHERE merged_into=? AND is_aggregate=0 ORDER BY started_at,id''', (old['id'],)).fetchall()
            available = [r for r in originals if r['entry_id'] and c.execute(
                'SELECT 1 FROM media_entries WHERE id=?', (r['entry_id'],)).fetchone()]
            if not available:
                continue  # Never hide the only surviving indexed copy.
            anchor = available[0]
            for row in originals:
                c.execute('UPDATE live_recordings SET merged_into=NULL WHERE id=?', (row['id'],))
                c.execute('UPDATE media_entries SET superseded_by=NULL WHERE id=?', (row['entry_id'],))
                changed.add(row['id'])
            c.execute('UPDATE live_recordings SET merged_into=? WHERE id=?', (anchor['id'], old['id']))
            c.execute('UPDATE media_entries SET superseded_by=? WHERE id=?', (anchor['entry_id'], old['entry_id']))
            changed.add(old['id'])

        groups = c.execute('''SELECT sub_id,session_id FROM live_recordings
            WHERE is_aggregate=0 AND status IN ('completed','stopped','interrupted')
            AND sub_id IS NOT NULL AND session_id<>'' GROUP BY sub_id,session_id
            HAVING COUNT(entry_id)>1''').fetchall()
        for group in groups:
            records = [dict(row) for row in c.execute('''SELECT r.*,e.availability FROM live_recordings r
                LEFT JOIN media_entries e ON e.id=r.entry_id
                WHERE sub_id=? AND session_id=? AND is_aggregate=0
                AND status IN ('completed','stopped','interrupted') ORDER BY r.started_at,r.id''', tuple(group))]
            playable = [r for r in records if r['entry_id'] and r['availability']]
            if len(playable) < 2:
                continue
            first, last = records[0], records[-1]
            anchor = next((r for r in playable if r['availability']=='present'), playable[0])
            identity = uuid.uuid5(uuid.NAMESPACE_URL, f"live-collection:{group['sub_id']}:{group['session_id']}").hex
            old = c.execute('SELECT * FROM live_recordings WHERE id=?', (identity,)).fetchone()
            has_gaps = any(r['status']=='interrupted' or r['chat_status']!='captured' for r in records)
            has_gaps |= any((_seconds(a['finished_at'], b['started_at']) or 0)>2 for a,b in zip(records, records[1:]))
            value = dict(id=identity,sub_id=first['sub_id'],room_url=first['room_url'],
                session_id='collection:'+first['session_id'],previous_id=None,author=first['author'],
                title=f"{first['title'] or '直播录播'} · 录播合集（{len(playable)} 段）",
                status=last['status'],reason=last['reason'],started_at=first['started_at'],finished_at=last['finished_at'],
                output_rel=anchor['output_rel'],entry_id=anchor['entry_id'],
                bytes=sum(r['bytes'] for r in playable),duration_seconds=sum(r['duration_seconds'] for r in playable),
                chat_messages=sum(r['chat_messages'] for r in records),chat_status='captured_with_gaps' if has_gaps else 'captured',
                error='分段保存，存在录制或弹幕缺口；原始时间与中断记录保留' if has_gaps else '',
                quality=first['quality'],broadcast_started_at=first.get('broadcast_started_at',''),
                is_aggregate=1,is_collection=1,merged_into=None)
            if not old or any(old[key]!=val for key,val in value.items()):
                columns = list(value)
                c.execute('INSERT INTO live_recordings ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+') '
                    'ON CONFLICT(id) DO UPDATE SET '+','.join(key+'=excluded.'+key for key in columns if key!='id'),tuple(value.values()))
                changed.add(identity)
            # context.md in older recordings labels a UTC recording timestamp
            # as publication text. The collection uses its durable recording
            # clock for ordering/date filters; the original metadata stays intact.
            updated = c.execute('''UPDATE media_entries SET superseded_by=NULL,sort_at=?,date_source='recorded'
                WHERE id=? AND (superseded_by IS NOT NULL OR sort_at IS NOT ? OR date_source<>'recorded')''',
                (first['started_at'],anchor['entry_id'],first['started_at']))
            if updated.rowcount:
                changed.add(identity)
            for record in records:
                if record['merged_into'] != identity:
                    c.execute('UPDATE live_recordings SET merged_into=? WHERE id=?', (identity,record['id']))
                    changed.add(record['id'])
                if record['entry_id'] != anchor['entry_id']:
                    c.execute('UPDATE media_entries SET superseded_by=? WHERE id=?', (anchor['entry_id'],record['entry_id']))
                # Old aggregate links follow their original anchor into its new
                # collection; no physical files or existing logs are rewritten.
                for prior in c.execute('SELECT id,entry_id FROM live_recordings WHERE is_aggregate=1 AND is_collection=0 AND merged_into=?', (record['id'],)).fetchall():
                    c.execute('UPDATE live_recordings SET merged_into=? WHERE id=?', (identity,prior['id']))
                    c.execute('UPDATE media_entries SET superseded_by=? WHERE id=?', (anchor['entry_id'],prior['entry_id']))
                    changed.add(prior['id'])
    return changed


async def reconcile():
    import asyncio
    from . import live_store, live_tasks, task_manager
    changed = await asyncio.to_thread(refresh)
    for identity in sorted(changed):
        await live_tasks.sync_record(live_store.get_record(identity))
    if changed:
        await task_manager.broadcast({'type':'library_updated'})


def enrich_summaries(items, *, database=None):
    if not items:
        return
    with db.connect(database) as c:
        # The catalog is also used independently for preflight/legacy indexes.
        if not c.execute("SELECT 1 FROM pragma_table_info('live_recordings') WHERE name='is_collection'").fetchone():
            return
        rows = c.execute('''SELECT g.*,COUNT(r.entry_id) part_count FROM live_recordings g
            LEFT JOIN live_recordings r ON r.merged_into=g.id AND r.is_aggregate=0
            WHERE g.is_collection=1 AND g.entry_id IN ('''+','.join('?' for _ in items)+') GROUP BY g.id',
            [item['id'] for item in items]).fetchall()
    groups = {row['entry_id']:row for row in rows}
    for item in items:
        group = groups.get(item['id'])
        if group:
            item.update(collection={'id':group['id'],'part_count':group['part_count'],
                'started_at':group['started_at'],'finished_at':group['finished_at'],
                'duration_seconds':group['duration_seconds'],'chat_messages':group['chat_messages']},
                text_preview=group['title'],size=group['bytes'],video_count=group['part_count'],
                media_count=group['part_count'],counts={'photos':0,'lives':0,'videos':group['part_count']})


def enrich_entry(entry):
    """Compose playback URLs from indexed source entries; never forge rel paths."""
    from . import catalog
    if not entry:
        return entry
    requested_id = entry['id']
    with db.connect() as c:
        source = c.execute('SELECT * FROM live_recordings WHERE entry_id=? AND is_collection=0 ORDER BY is_aggregate LIMIT 1', (requested_id,)).fetchone()
        group = c.execute('''SELECT * FROM live_recordings WHERE is_collection=1
            AND (entry_id=? OR id=?) LIMIT 1''', (requested_id,source['merged_into'] if source else None)).fetchone()
        if not group:
            return entry
        segments = parts(c, group['id'])
    result = catalog.get_entry(group['entry_id'])
    if not result:
        return entry
    enrich_summaries([result])
    gallery, public_parts, initial = [], [], 0
    for segment in segments:
        public = {key:segment[key] for key in ('id','entry_id','started_at','finished_at','duration_seconds',
            'chat_messages','chat_status','status','reason','error','gap_before_seconds','gap_basis','root_id','rel_dir','availability')}
        media = catalog.get_entry(segment['entry_id']) if segment['entry_id'] else None
        public['playable'] = bool(media and media['availability']=='present' and media['gallery'])
        public['gallery_index'] = len(gallery) if public['playable'] else None
        if public['playable']:
            if segment['entry_id']==requested_id:
                initial=len(gallery)
            for asset in media['gallery']:
                gallery.append(dict(asset,source_entry_id=media['id'],root_id=media['root_id'],rel_dir=media['rel_dir'],
                    segment=public))
        public_parts.append(public)
    result.update(gallery=gallery,initial_gallery_index=initial,text=group['title'])
    result['collection'].update(parts=public_parts,timezone='Asia/Shanghai',
        alignment='Each source retains its own video_time_ms and received_at; no continuous merged clock.')
    return result
