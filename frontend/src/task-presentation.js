export function taskAuthor(task) {
  const values = [task.author, task.metadata?.subscription?.author, task.metadata?.live?.author]
  return values.find(value => typeof value === 'string' && value.trim())?.trim() || ''
}

export function taskTitle(task) {
  const values = [task.display_title, task.metadata?.subscription?.title, task.params?.title]
  return values.find(value => typeof value === 'string' && value.trim())?.trim()
    || (task.metadata?.live ? '直播录制' : '内容标题待获取')
}

export function taskPlatform(task) {
  return task.metadata?.live ? 'douyin' : task.script_id
}

export function taskContentTime(task) {
  if (task.metadata?.live) {
    return task.broadcast_started_at
      ? { label: '开播', value: task.broadcast_started_at }
      : { label: '录制开始', value: task.recording_started_at || task.started_at || null }
  }
  return { label: '发布', value: task.published_at || task.metadata?.subscription?.published_at || null }
}

export function mergeTaskSummary(previous, incoming) {
  // Queue events omit catalog details. Keep them across status/progress updates.
  const result = {...incoming, author:incoming.author || previous?.author || taskAuthor(incoming) || taskAuthor(previous || {}) || null}
  for (const key of ['author_avatar_url', 'display_title', 'published_at', 'broadcast_started_at', 'recording_started_at']) {
    result[key] = incoming[key] || previous?.[key] || null
  }
  return result
}
