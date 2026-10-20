const coverageReasons = {
  network_error: '后续分页网络中断或请求超时，已取得的内容保留，请稍后重试',
  network_changed: '网络连接发生变化，已取得的内容保留，请网络稳定后重新扫描',
  network_unavailable: '网络暂时不可用，已取得的内容保留，请检查连接后重新扫描',
  navigation_failed: '主页未能正常打开，请检查网络后重新扫描',
  rate_limited: '平台请求被限流，请降低扫描频率并稍后重试',
  platform_error: '平台响应异常，已取得的内容保留，本轮回溯未完成',
  platform_limited: '平台限制后续分页，已取得的内容保留，请稍后重试',
  baseline_window: '已读取首次基线当前页',
  platform_end: '平台返回已到内容末尾',
  overlap_covered: '已覆盖设定的重叠回溯窗口',
  page_limit: '已达到本轮最大页数',
  item_limit: '已达到本轮最大条目数',
  time_limit: '已达到本轮扫描时间上限',
  pagination_stalled: '分页未取得新内容，可能受到平台限制或页面变化影响',
  legacy_scanner: '旧扫描器返回结果，未记录分页覆盖细节',
}

export function coverageReason(coverage, platform) {
  if (coverage?.reason === 'login_required') {
    const name = { weibo: '微博', douyin: '抖音' }[platform] || '对应平台'
    return `平台限制后续分页，请登录${name}后重新扫描`
  }
  return coverageReasons[coverage?.reason] || (coverage?.complete ? '扫描器未提供具体覆盖说明' : '本轮扫描未覆盖完整窗口，请查看扫描记录后重试')
}

export function scanCoverageNotice(coverage, platform) {
  return coverage?.complete === false ? `覆盖不足：${coverageReason(coverage, platform)}` : ''
}

export function scanStatusText(status, coverage) {
  if (['ok', 'success', 'partial'].includes(status) && coverage?.complete === false) return '覆盖不足'
  return { ok: '完成', success: '完成', partial: '覆盖不足', error: '失败', failed: '失败',
    baseline: '基线已建立', queued: '排队', running: '执行中', skipped: '已合并或跳过',
    cancelled: '已取消', interrupted: '中断', missed: '错过', retry: '等待重试' }[status] || status
}

export function coverageText(coverage, platform) {
  if (typeof coverage?.complete !== 'boolean') return '覆盖情况：此记录未保存覆盖信息'
  const title = coverage.complete ? '已覆盖本轮扫描目标' : '覆盖不足'
  const counts = [Number.isFinite(coverage.pages) ? `${coverage.pages} 页` : '',
    Number.isFinite(coverage.items) ? `${coverage.items} 条` : ''].filter(Boolean)
  return `${title}：${coverageReason(coverage, platform)}${counts.length ? ' · ' + counts.join(' / ') : ''}`
}

export function terminalScanKey(scan) {
  return scan?.scan_id && ['success', 'partial', 'failed', 'cancelled', 'interrupted'].includes(scan.status)
    ? `${scan.scan_id}:${scan.status}:${scan.revision || 0}` : ''
}

export function scanBadge(sub, scanning = false) {
  if (scanning || sub.scan?.status === 'running') return {text:'扫描中', kind:'scanning'}
  if (['success','partial'].includes(sub.scan?.status) && sub.scan.downloaded_count > 0) {
    return {text:'有新内容', kind:'new', title:`本轮已成功下载 ${sub.scan.downloaded_count} 条内容`}
  }
  return null
}
