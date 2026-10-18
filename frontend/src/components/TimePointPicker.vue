<script setup>
import { computed } from 'vue'
const props=defineProps({modelValue:{type:String,default:'09:00'},disabled:Boolean})
const emit=defineEmits(['update:modelValue'])
const times=computed(()=>props.modelValue.split(/[,，\s]+/).filter(Boolean))
const hours=Array.from({length:24},(_,i)=>String(i).padStart(2,'0'))
const minutes=Array.from({length:60},(_,i)=>String(i).padStart(2,'0'))
function change(index,part,value){const next=[...times.value],bits=next[index].split(':');bits[part]=value;next[index]=bits.join(':');emit('update:modelValue',next.join(', '))}
function remove(index){emit('update:modelValue',times.value.filter((_,i)=>i!==index).join(', '))}
function add(){const value=hours.map(h=>h+':00').find(v=>!times.value.includes(v));if(value)emit('update:modelValue',[...times.value,value].join(', '))}
</script>
<template>
  <fieldset class="time-points" :disabled="disabled"><legend>执行时间</legend>
    <div class="time-list"><div v-for="(value,index) in times" :key="index" class="time-point">
      <select class="select" :aria-label="`时间 ${index+1}：小时`" :value="value.split(':')[0]" @change="change(index,0,$event.target.value)"><option v-for="h in hours" :key="h">{{h}}</option></select><span>:</span>
      <select class="select" :aria-label="`时间 ${index+1}：分钟`" :value="value.split(':')[1]" @change="change(index,1,$event.target.value)"><option v-for="m in minutes" :key="m">{{m}}</option></select>
      <button type="button" class="remove-time" :aria-label="`移除时间 ${value}`" :disabled="times.length<=1" @click="remove(index)">×</button>
    </div></div>
    <button type="button" class="btn btn-ghost btn-sm" :disabled="times.length>=24" @click="add">＋ 添加时间</button>
  </fieldset>
</template>
<style scoped>
.time-points{border:0;padding:0;margin:0;min-width:0}.time-points legend{font-size:12px;color:var(--text-2);margin-bottom:8px}.time-list{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}.time-point{display:flex;align-items:center;gap:6px;padding:5px;background:var(--bg,#fff);border:1px solid var(--border);border-radius:10px}.time-point .select{width:68px;padding:7px;font-size:13px}.remove-time{border:0;background:transparent;color:var(--text-2);font-size:20px;cursor:pointer;padding:2px 7px}.remove-time:disabled{opacity:.3}
</style>
