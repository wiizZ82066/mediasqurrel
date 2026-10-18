<script setup>
import { computed, ref } from 'vue'
import TimePointPicker from './TimePointPicker.vue'
const props=defineProps({modelValue:{type:Object,required:true},disabled:Boolean})
const emit=defineEmits(['update:modelValue'])
const d=computed(()=>props.modelValue)
const selectedDate=ref('')
const dates=computed(()=>String(d.value.dates||'').split(/[,，\s]+/).filter(Boolean))
const days=['周一','周二','周三','周四','周五','周六','周日']
const zones=computed(()=>[...new Set(['Asia/Shanghai',Intl.DateTimeFormat().resolvedOptions().timeZone,d.value.timezone,'Asia/Tokyo','America/New_York','Europe/London','UTC'].filter(Boolean))])
function set(key,value){emit('update:modelValue',{...d.value,[key]:value})}
function weekday(index){const next=new Set(d.value.weekdays);next.has(index)?next.delete(index):next.add(index);set('weekdays',[...next].sort())}
function addDate(){if(selectedDate.value)set('dates',[...new Set([...dates.value,selectedDate.value])].sort().join('\n'))}
function showCalendar(event){try{event.target.showPicker?.()}catch{}}
</script>
<template><fieldset class="calendar-fields" :disabled="disabled">
  <div class="calendar-grid"><label>重复方式<select class="select" :value="d.kind" @change="set('kind',$event.target.value)"><option value="daily">每天</option><option value="weekly">按星期</option><option value="dates">指定日期</option><option value="repeat">周期重复</option></select></label><label>时区<select class="select" :value="d.timezone" @change="set('timezone',$event.target.value)"><option v-for="zone in zones" :key="zone">{{zone}}</option></select></label><label>开始日期<input class="input" type="date" required :value="d.start_date" @click="showCalendar" @input="set('start_date',$event.target.value)"></label></div>
  <TimePointPicker :model-value="d.times" @update:model-value="set('times',$event)"/>
  <div v-if="d.kind==='weekly'" class="day-buttons" role="group" aria-label="选择星期"><button v-for="(day,index) in days" :key="day" type="button" class="btn btn-ghost btn-sm" :aria-pressed="d.weekdays.includes(index)" @click="weekday(index)">{{day}}</button></div>
  <div v-if="d.kind==='dates'" class="date-choices"><label>选择日期<input v-model="selectedDate" class="input" type="date" @click="showCalendar"></label><button type="button" class="btn btn-ghost btn-sm" :disabled="!selectedDate || dates.length>=1000" @click="addDate">添加日期</button><div class="date-chips"><button v-for="date in dates" :key="date" type="button" class="btn btn-ghost btn-sm" :aria-label="`移除日期 ${date}`" @click="set('dates',dates.filter(v=>v!==date).join('\n'))">{{date}} ×</button></div></div>
  <div v-if="d.kind==='repeat'" class="calendar-grid"><label>每隔<select class="select" :value="d.every" @change="set('every',Number($event.target.value))"><option v-for="n in [...new Set([...Array.from({length:60},(_,i)=>i+1),Number(d.every)])].sort((a,b)=>a-b)" :key="n" :value="n">{{n}}</option></select></label><label>周期单位<select class="select" :value="d.unit" @change="set('unit',$event.target.value)"><option value="days">天</option><option value="weeks">周</option><option value="months">月</option></select></label></div>
  <p v-if="d.kind==='repeat' && d.unit==='months'" class="hint">按开始日期的日号重复，较短月份使用该月最后一天。</p>
  <details class="limits"><summary>结束条件（默认持续执行）</summary><div class="calendar-grid"><label>执行次数上限（可留空）<input class="input" type="number" min="1" max="10000" :value="d.count" @input="set('count',$event.target.value)"></label><label>结束日期（可留空）<input class="input" type="date" :value="d.end_date" @click="showCalendar" @input="set('end_date',$event.target.value)"></label></div></details>
  <p class="hint">多个时间点可逐个添加。退出或休眠期间错过的时间合并检查一次；次数上限按计划时点计算。</p>
</fieldset></template>
<style scoped>
.calendar-fields{border:0;padding:0;margin:0;min-width:0}.calendar-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(180px,100%),1fr));gap:12px;margin:14px 0}.calendar-grid label,.date-choices label{font-size:12px;color:var(--text-2);min-width:0}.calendar-fields .input,.calendar-fields .select{display:block;margin-top:6px;font-size:13px}.day-buttons,.date-choices,.date-chips{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0;align-items:flex-end}.day-buttons [aria-pressed=true]{background:var(--blue);color:white}.date-chips{flex-basis:100%;margin:0}.limits{font-size:12px;margin-top:16px}.limits summary{cursor:pointer}.hint{font-size:12px;color:var(--text-2);line-height:1.7;margin:10px 0}
</style>
