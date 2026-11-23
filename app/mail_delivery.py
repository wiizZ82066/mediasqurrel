"""Deliver work attachments and recording receipts on one background mail worker."""
import asyncio
from contextlib import closing
from email import policy
from email.message import EmailMessage
from email.utils import formatdate
import hashlib
import smtplib
import sqlite3
import ssl
import threading
import time

from fastapi import HTTPException

from . import config, mail_media, mail_store

_wake = None
_worker = None
_enabled = False
_closing = threading.Event()
_pending_recordings = set()
_working = False


def busy():
    return _working or bool(_pending_recordings)


def _envelope(subject, job, sender, recipient, ordinal):
    message = EmailMessage(policy=policy.SMTP)
    message['Subject'] = subject
    message['From'], message['To'] = sender, recipient
    message['Date'] = formatdate(localtime=True)
    identity = hashlib.sha256(f"{job['id']}:{ordinal}".encode()).hexdigest()
    message['Message-ID'] = f'<{identity}@mediasquirrel.local>'
    return message


def build_message(work, attachments, volumes, job, sender, recipient, ordinal, total):
    prefix = '测试作品' if job['sample'] else '下载完成'
    batch = f'（{ordinal}/{total}）' if total > 1 else ''
    message = _envelope(f'{prefix}{batch} · {work.author} · {work.title}', job, sender, recipient, ordinal)
    lines = [f'{work.author} · {work.platform}', '', work.text[:4000] or work.title]
    if len(work.text) > 4000:
        lines.append('完整正文见附件「作品正文.txt」。')
    if total > 1:
        lines += ['', f'这条作品分为 {total} 封，这是第 {ordinal} 封。带有“分段”的 MP4 可按编号独立播放。']
    if volumes:
        lines += ['', '以下原文件超过单个附件容量，按编号发送完整字节。收齐后依次合并同名的 .001、.002 等分件，即可还原原文件。',
                  *sorted(volumes)]
    # The mail contains post text and media, not the archive's metadata/paths/logs.
    message.set_content('\n'.join(lines)[:30000])
    for part in attachments:
        main, sub = part.mime.split('/', 1)
        message.add_attachment(part.read(), maintype=main, subtype=sub, filename=part.name)
    return message


def open_smtp(p):
    client = smtplib.SMTP_SSL('smtp.qq.com', 465, timeout=35, local_hostname='mediasquirrel.local',
                              context=ssl.create_default_context())
    try:
        client.ehlo()
        client.login(p['sender'], mail_store.unseal(p['sealed']))
        return client
    except BaseException:
        client.close()
        raise


def failure(job, reason, *, retryable=False, uncertain=False):
    attempts = job['attempts'] + 1
    retry = retryable and attempts < 6 and not uncertain
    mail_store.update(job['id'], state='uncertain' if uncertain else 'queued' if retry else 'failed',
                      reason=reason, attempts=attempts, due=time.time() + min(3600, 60 * 2 ** min(attempts - 1, 6)))


def _messages(job, preferences, work, limit):
    sender, recipient = preferences['sender'], preferences['recipient']
    if job.get('recording_body') is not None:
        message = _envelope('录播完成 · ' + job['caption'], job, sender, recipient, 1)
        message.set_content(job['recording_body'])
        yield 1, 1, message
        return
    with mail_media.workspace() as scratch:
        groups, fingerprint, volumes = mail_media.batches(work, limit, scratch)
        if job['done'] and (fingerprint != job['fingerprint'] or job['total'] != len(groups)):
            raise ValueError('已发送部分附件后，作品或分件发生了变化。请核对已收到的附件。')
        mail_store.update(job['id'], total=len(groups), wire_limit=limit, fingerprint=fingerprint)
        for ordinal in range(job['done'] + 1, len(groups) + 1):
            if _closing.is_set():
                mail_store.update(job['id'], state='queued', due=time.time())
                return
            yield ordinal, len(groups), build_message(work, groups[ordinal - 1], volumes, job, sender,
                                                      recipient, ordinal, len(groups))


def deliver(job, preferences):
    key, client, handed_off = job['id'], None, False
    try:
        work = None
        if job.get('recording_body') is None:
            task = mail_media.task_by_id(job['task_id'])
            if not task:
                failure(job, '当前数据库中没有这条可投递的下载记录，请确认媒体盘已连接。', retryable=True)
                return
            work = mail_media.resolve(task)
            mail_store.update(key, caption=f'{work.author} · {work.title}')
        client = open_smtp(preferences)
        advertised = client.esmtp_features.get('size', '').strip()
        limit = min(mail_store.LIMIT, int(advertised)) if advertised.isdecimal() and int(advertised) else mail_store.LIMIT
        if job['wire_limit']:
            if limit < job['wire_limit'] and job['done']:
                raise ValueError('邮箱容量规则已改变，请先核对已收到的分件。')
            limit = min(limit, job['wire_limit'])
        with closing(_messages(job, preferences, work, limit)) as messages:
            for ordinal, total, message in messages:
                if _closing.is_set():
                    mail_store.update(key, state='queued', due=time.time())
                    return
                data = message.as_bytes()
                del message
                if len(data) > limit:
                    raise ValueError('邮件编码后超出邮箱容量，尚未投递这一封。')
                mail_store.update(key, state='sending', reason='')
                handed_off = True
                refused = client.sendmail(preferences['sender'], [preferences['recipient']], data)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
                del data
                mail_store.update(key, state='sent' if ordinal == total else 'preparing', done=ordinal, attempts=0, reason='')
                handed_off = False
    except smtplib.SMTPAuthenticationError:
        failure(job, 'QQ 邮箱未接受授权码，请检查发件地址和 SMTP 服务，保存新授权码后重试。')
    except smtplib.SMTPRecipientsRefused as error:
        transient = all(400 <= value[0] < 500 for value in error.recipients.values())
        failure(job, 'QQ 邮箱未接受收件地址，请检查地址与收件容量。', retryable=transient)
    except smtplib.SMTPResponseException as error:
        failure(job, f'QQ 邮箱拒绝了本次投递（SMTP {error.smtp_code}）。请检查发件限制与邮箱容量。',
                retryable=400 <= error.smtp_code < 500)
    except (ConnectionError, TimeoutError, smtplib.SMTPServerDisconnected, OSError, sqlite3.Error):
        failure(job, '投递时连接中断，请先检查收件箱是否已收到这一封。' if handed_off else
                '暂时无法读取作品或连接 QQ 邮箱，稍后会重新尝试。', retryable=True, uncertain=handed_off)
    except HTTPException:
        failure(job, '作品文件暂不可读取，请确认媒体盘和作品位置。', retryable=True)
    except ValueError as error:
        # Only our actionable, controlled messages may reach the status UI.
        reason = str(error)
        if len(reason) > 160 or not reason or reason[0].isascii():
            reason = '作品清单或邮箱配置无法使用，请检查后重试。'
        failure(job, reason)
    except Exception:
        failure(job, '邮件投递未完成，请核对收件箱后重试。', uncertain=handed_off)
    finally:
        if client:
            client.close()


def notify():
    if _wake is not None:
        _wake.set()


async def completed(task):
    if _worker is not None and mail_media.eligible(task):
        await asyncio.to_thread(mail_store.capture, task)
        notify()


def recording_saved(identity):
    if _worker is not None and _enabled:
        _pending_recordings.add(identity)
        notify()


async def loop():
    global _working
    recover = True
    while not _closing.is_set():
        if config.MAINTENANCE_ACTIVE:
            await asyncio.sleep(.5)
            continue
        _working = True
        _wake.clear()
        try:
            if recover and _enabled:
                await asyncio.to_thread(mail_store.recover)
                recover = False
            while _pending_recordings:
                await asyncio.to_thread(mail_store.capture_recordings, _pending_recordings.pop())
        except (OSError, sqlite3.Error):
            recover = True  # Recover the durable archive event even if the removable drive was offline.
        try:
            job, value = await asyncio.to_thread(mail_store.claim)
            if job:
                await asyncio.to_thread(deliver, job, value)
                continue
            timeout = value
        except (OSError, sqlite3.Error):
            timeout = 60
        finally:
            _working = False
        if recover and _enabled:
            timeout = 60 if timeout is None else min(timeout, 60)
        if not _closing.is_set():
            try:
                await asyncio.wait_for(_wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass


async def start():
    global _wake, _worker, _enabled
    try:
        await asyncio.to_thread(mail_store.initialize)
        _enabled = (await asyncio.to_thread(mail_store.public))['enabled']
    except (OSError, sqlite3.Error):
        _enabled = False
        return  # The local settings endpoint reports the error; downloading still works.
    try:
        await asyncio.to_thread(mail_media.release_interrupted)
    except (OSError, ValueError):
        pass  # An unavailable cache must not stop downloads or recording.
    _closing.clear()
    _wake = asyncio.Event()
    _worker = asyncio.create_task(loop())


async def stop():
    global _worker, _wake
    _closing.set()
    notify()
    if _worker:
        await _worker
    _pending_recordings.clear()
    _worker, _wake = None, None


async def save(sender, recipient, code, enabled):
    global _enabled
    result = await asyncio.to_thread(mail_store.save, sender, recipient, code, enabled)
    _enabled = result['enabled']
    notify()
    return result
