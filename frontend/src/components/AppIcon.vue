<script setup>
defineProps({ name: { type: String, required: true }, size: { type: [Number, String], default: 22 } })
</script>
<template>
  <svg :width="size" :height="size" viewBox="0 0 48 48" fill="none" aria-hidden="true" class="app-icon" stroke-linecap="round" stroke-linejoin="round">
    <g v-if="name === 'brand'">
      <path d="M25 39C6 47 1 30 6 19 10 10 22 8 24 17 26 26 13 29 13 20 8 30 18 34 24 27" fill="#ce8751"/>
      <path d="M25 14 25 5 33 11 39 10 42 18 37 25 37 35 41 40 23 40 22 29 18 25 21 17Z" fill="#a45b34"/>
      <path d="M31 23C26 25 26 34 31 37H36L35 25Z" fill="#f4d5ae"/>
      <path d="m26 8 4 5-4 2" fill="#edb184"/><circle cx="35" cy="17" r="2" fill="#322a28"/>
      <path d="m40 20 4 1-4 3" fill="#322a28"/><path d="M29 29h8" stroke="#a45b34" stroke-width="3"/>
      <path d="M34 29c0-5 7-5 7 0 0 4-3 6-3 6s-4-2-4-6" fill="#dea654"/><path d="M34 28h7" stroke="#735439" stroke-width="3"/>
    </g>
    <g v-else-if="name === 'download'" stroke="#2779c9" stroke-width="3.5"><path d="M7 29v10h34V29" fill="#d9eaf8"/><path d="M24 7v24m-9-9 9 9 9-9"/></g>
    <g v-else-if="name === 'tasks'" stroke="#9070bc" stroke-width="3"><rect x="9" y="9" width="30" height="33" rx="5" fill="#eee5f8"/><rect x="17" y="5" width="14" height="8" rx="3" fill="#d2bce9"/><path d="m16 23 3 3 5-6m3 4h5m-16 9h16"/></g>
    <g v-else-if="name === 'library'" stroke="#438f73" stroke-width="3"><rect x="6" y="8" width="36" height="32" rx="5" fill="#def0e4"/><circle cx="16" cy="19" r="4" fill="#f6cc73" stroke="none"/><path d="m7 35 11-10 8 6 7-11 8 14" fill="#94c8af"/></g>
    <g v-else-if="name === 'subs'" stroke="#cf8a38" stroke-width="3"><path d="M13 30V20a11 11 0 0 1 22 0v10l5 6H8Z" fill="#f8e8cc"/><path d="M19 41h10M24 5v4"/></g>
    <g v-else-if="name === 'settings'" stroke="#758394" stroke-width="3"><path d="m20 5-2 6-6 1-5 8 4 5-1 6 7 7 6-2 6 2 8-7-1-6 4-5-5-8-6-1-2-6Z" fill="#e3e9ef"/><circle cx="24" cy="23" r="7" fill="#fff"/></g>
    <g v-else-if="name === 'folder'" stroke="#c99136" stroke-width="3"><path d="M5 13h15l4 5h19v21H5Z" fill="#f7dba4"/><path d="M5 24h38"/></g>
    <g v-else-if="name === 'paste'" stroke="#438f73" stroke-width="3"><rect x="10" y="10" width="28" height="32" rx="4" fill="#e0efe5"/><rect x="17" y="6" width="14" height="9" rx="3" fill="#b2d6c0"/><path d="M18 25h12m-12 7h8"/></g>
    <!-- IconPark Scanning, Apache-2.0; modified colors. See public/licenses. -->
    <g v-else-if="name === 'scan'" stroke-width="4"><path d="M16 6H8C6.89543 6 6 6.89543 6 8V16M16 42H8C6.89543 42 6 41.1046 6 40V32M32 42H40C41.1046 42 42 41.1046 42 40V32M32 6H40C41.1046 6 42 6.89543 42 8V16" stroke="#648cca"/><path d="M34 24H14M27 16H21M27 32H21" stroke="#60a98a"/></g>
    <g v-else-if="name === 'search'" stroke="#647da6" stroke-width="4"><circle cx="21" cy="21" r="12" fill="#e1eaf7"/><path d="m30 30 10 10"/></g>
    <g v-else-if="name === 'play'" stroke="#438f73" stroke-width="3"><circle cx="24" cy="24" r="19" fill="#e0efe5"/><path d="m20 15 13 9-13 9Z" fill="#438f73"/></g>
    <g v-else-if="name === 'up'" stroke="#647da6" stroke-width="4"><path d="m12 27 12-12 12 12M24 16v24"/></g>
    <g v-else-if="name === 'close'" stroke="#758394" stroke-width="4"><path d="m14 14 20 20m0-20-20 20"/></g>
    <g v-else-if="name === 'person'" stroke="#9070bc" stroke-width="3"><circle cx="24" cy="15" r="8" fill="#eee5f8"/><path d="M8 41c0-18 32-18 32 0Z" fill="#d2bce9"/></g>
    <g v-else-if="name === 'calendar'" stroke="#bc786c" stroke-width="3"><rect x="7" y="10" width="34" height="31" rx="5" fill="#fae5df"/><path d="M7 21h34M16 6v10M32 6v10M16 29h6m6 0h4m-16 6h6"/></g>
    <g v-else stroke="#647da6" stroke-width="3"><circle cx="24" cy="24" r="18" fill="#e1eaf7"/><path d="M24 22v12M24 14v1"/></g>
  </svg>
</template>
<style scoped>.app-icon { display:inline-block; flex-shrink:0; vertical-align:middle; }</style>
