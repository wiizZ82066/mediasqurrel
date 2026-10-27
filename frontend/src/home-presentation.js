// Keep dashboard times consistent with the subscription and recording timeline.
export function homeTime(value, { date = false, zone = 'Asia/Shanghai' } = {}) {
  if (!value || !Number.isFinite(new Date(value).getTime())) return '时间待确认'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: zone, ...(date ? { month: '2-digit', day: '2-digit' } : {}),
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(new Date(value))
}
export function nextScanLabel(scan, now = Date.now()) {
  if (!scan) return '尚未设置扫描计划'
  if (new Date(scan.due_at).getTime() <= now) return '等待执行 · 到期计划将合并检查'
  return `${homeTime(scan.due_at, { date: true, zone: scan.timezone || 'Asia/Shanghai' })} · ${scan.timezone || 'Asia/Shanghai'}`
}
export function homeThumbnail(entry) {
  if (!entry.cover) return ''
  return '/api/thumb?' + new URLSearchParams({ root_id: entry.root_id, p: `${entry.rel_dir}/${entry.cover}`, w: 480 })
}
