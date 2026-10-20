export const liveLabels = {
  disabled:'未开启录播', waiting:'等待检查', offline:'等待开播', capacity:'等待录制空位',
  error:'等待重试', checking:'检查直播中', connecting:'正在连接直播', recording:'正在录制',
  stalled:'视频暂未更新', finalizing:'正在保存', recovering:'正在恢复录播',
  completed:'下播后结束', stopped:'已手动停止', interrupted:'录制中断',
}
export function liveState(monitor) { return monitor?.runtime_status || monitor?.status || 'disabled' }
export function liveDuration(value) {
  const n=Math.max(0,Math.floor(Number(value)||0))
  return `${Math.floor(n/3600)}:${String(Math.floor(n/60)%60).padStart(2,'0')}:${String(n%60).padStart(2,'0')}`
}
export function liveSize(value) {
  const n=Math.max(0,Number(value)||0)
  return n>=1024**3 ? `${(n/1024**3).toFixed(2)} GB` : `${(n/1024**2).toFixed(1)} MB`
}
export function livePresentation(monitor) {
  const state=liveState(monitor), record=monitor?.record
  const active=['recording','connecting','stalled','finalizing'].includes(state)
  const tone=['error','stalled'].includes(state)?'warning':active?'recording':'neutral'
  let detail=''
  if(active && record) detail=`${liveDuration(record.duration_seconds)} · ${liveSize(record.bytes)} · 弹幕 ${record.chat?.messages || 0} 条`
  else if(state==='error') detail=monitor.error || '请展开查看失败原因'
  else if(monitor?.enabled && monitor.next_check_at) detail=`下次检查 ${new Date(monitor.next_check_at).toLocaleString('zh-CN',{hour12:false})}`
  else if(monitor?.enabled && !monitor.next_check_at) detail='当前检查计划已结束'
  return {state,active,tone,label:liveLabels[state] || '状态待确认',detail}
}
