<script setup>
import { computed } from 'vue'
import Avatar from './Avatar.vue'
import PlatformLogo from './PlatformLogo.vue'
import AppIcon from './AppIcon.vue'
import ContentText from './ContentText.vue'
import { mediaProfile } from '../viewer-presentation.js'

const props = defineProps({ entry: { type: Object, required: true }, dateText: String, dateLabel: String, showProfile: Boolean })
const profile = computed(() => mediaProfile(props.entry))
</script>

<template>
  <article class="media-details">
    <header v-if="showProfile" class="details-author">
      <div class="details-avatar">
        <Avatar :src="profile.avatarUrl" :name="entry.author" :size="48" />
        <PlatformLogo v-if="entry.platform" :platform="entry.platform" :size="20" />
      </div>
      <div class="details-identity">
        <h2>{{ entry.author || '作者未知' }}</h2>
        <p>{{ dateLabel }}<time v-if="dateText" :datetime="entry.sort_at">{{ dateText }}</time><span v-else>未知</span></p>
      </div>
    </header>
    <div class="details-reading">
      <p class="details-body" :class="{ 'is-empty': !entry.text && !entry.text_preview }"><ContentText :text="entry.text || entry.text_preview || '这条内容没有附带正文。'" :platform="entry.platform" /></p>
      <a v-if="profile.sourceUrl" class="details-source" :href="profile.sourceUrl" target="_blank" rel="noopener noreferrer">查看原文<AppIcon name="external" :size="16" /></a>
      <details class="details-archive">
        <summary>存档信息<AppIcon name="chevron-right" :size="14" /></summary>
        <dl>
          <template v-for="(value, key) in entry.meta" :key="key"><dt>{{ key }}</dt><dd><ContentText :text="String(value)" :platform="entry.platform" /></dd></template>
          <dt>目录</dt><dd>{{ entry.rel_dir }}</dd>
          <dt>{{ entry.collection ? '文件完整性' : '完整性' }}</dt>
          <dd>{{ entry.archive_status === 'complete' ? '已验证完整' : entry.archive_status === 'partial' ? '未完成' : '旧格式，尚未确认完整' }}<span v-if="entry.collection">（不代表直播全程）</span></dd>
        </dl>
      </details>
    </div>
  </article>
</template>

<style scoped>
.media-details { display:flex; flex-direction:column; min-height:0; height:100%; color:var(--viewer-text, #f5f5f7); }
.details-author { display:flex; align-items:center; gap:14px; flex-shrink:0; padding-bottom:24px; border-bottom:1px solid var(--viewer-line, #ffffff1a); }
.details-avatar { position:relative; flex-shrink:0; height:48px; }
.details-avatar .platform-logo { position:absolute; bottom:-3px; right:-4px; box-shadow:0 0 0 3px #303239; }
.details-identity { min-width:0; }
.details-identity h2 { font-size:17px; line-height:1.45; font-weight:600; letter-spacing:-.02em; overflow-wrap:anywhere; }
.details-identity p { display:flex; flex-wrap:wrap; gap:3px 6px; font-size:11px; line-height:1.7; color:var(--viewer-muted, #bcc0c9); margin-top:6px; }
.details-reading { min-height:0; overflow:auto; overscroll-behavior:contain; scrollbar-gutter:stable; padding-right:4px; }
.details-author + .details-reading { padding-top:24px; }
.details-body { white-space:pre-wrap; overflow-wrap:anywhere; font-size:15px; line-height:1.95; letter-spacing:.01em; }
.details-body.is-empty { color:var(--viewer-muted, #bcc0c9); }
.details-source { display:inline-flex; align-items:center; gap:6px; margin:24px 0 4px; font-size:12px; color:#b9d4ff; text-decoration:none; border-radius:4px; }
.details-source:hover { color:#fff; text-decoration:underline; text-underline-offset:4px; }
.details-archive { margin-top:28px; border-top:1px solid var(--viewer-line, #ffffff1a); padding:17px 0 4px; color:var(--viewer-muted, #bcc0c9); font-size:12px; }
.details-archive summary { display:flex; align-items:center; justify-content:space-between; gap:12px; cursor:pointer; list-style:none; padding:3px 0; border-radius:4px; }
.details-archive summary::-webkit-details-marker { display:none; }
.details-archive summary svg { transition:transform 180ms ease; }
.details-archive[open] summary svg { transform:rotate(90deg); }
.details-archive dl { display:grid; grid-template-columns:minmax(55px,auto) minmax(0,1fr); gap:10px 16px; margin-top:20px; font-size:11px; line-height:1.7; }
.details-archive dd { color:var(--viewer-text, #f5f5f7); overflow-wrap:anywhere; white-space:pre-wrap; }
.details-source:focus-visible,.details-archive summary:focus-visible { outline:2px solid #b9d4ff; outline-offset:5px; }
</style>
