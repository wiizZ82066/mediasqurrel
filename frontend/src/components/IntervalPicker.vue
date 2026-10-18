<script setup>
import { computed, ref, watch } from 'vue'
const props=defineProps({modelValue:{type:Number,required:true},min:{type:Number,default:60},max:{type:Number,default:86400},disabled:Boolean,label:{type:String,default:'检查间隔'}})
const emit=defineEmits(['update:modelValue'])
const chooseUnit=value=>value%3600===0?3600:value%60===0?60:1
const unit=ref(chooseUnit(props.modelValue))
watch(()=>props.modelValue,value=>{if(!Number.isInteger(value/unit.value))unit.value=chooseUnit(value)})
const units=computed(()=>[{value:1,label:'秒'},{value:60,label:'分钟'},{value:3600,label:'小时'},{value:86400,label:'天'}].filter(v=>v.value<=props.max && v.value*60>=props.min))
const values=computed(()=>[...new Set([...Array.from({length:60},(_,i)=>i+1),props.modelValue/unit.value])].filter(v=>v>0 && v*unit.value>=props.min && v*unit.value<=props.max).sort((a,b)=>a-b))
function changeUnit(event){unit.value=Number(event.target.value);const count=Math.max(1,Math.ceil(props.min/unit.value));emit('update:modelValue',Math.min(props.max,count*unit.value))}
</script>
<template><div class="interval-picker" :aria-label="label"><span>{{label}}</span><span>每</span><select class="select" :aria-label="`${label}数值`" :value="modelValue/unit" :disabled="disabled" @change="emit('update:modelValue',Number($event.target.value)*unit)"><option v-for="n in values" :key="n" :value="n">{{n}}</option></select><select class="select" :aria-label="`${label}单位`" :value="unit" :disabled="disabled" @change="changeUnit"><option v-for="u in units" :key="u.value" :value="u.value">{{u.label}}</option></select></div></template>
<style scoped>.interval-picker{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:13px}.interval-picker .select{width:auto;min-width:70px;padding:8px;font-size:13px}</style>
