export function parseDownloadTags(value) {
  const tags = String(value || '').split(/[,，\n]+/).map(tag => tag.trim()).filter(Boolean)
  if (tags.length > 20) throw new Error('最多填写 20 个标签。')
  if (tags.some(tag => tag.replace(/^#|#$/g, '').length > 80)) throw new Error('每个标签最多 80 字。')
  return { tags }
}

export function downloadFilterLabel(value) {
  const tags = value?.tags || []
  return tags.length ? '命中任一标签：' + tags.map(tag => '#' + tag).join('、') : '下载全部新内容'
}
