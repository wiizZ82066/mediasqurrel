import assert from 'node:assert/strict'
import fs from 'node:fs'
import {createRequire} from 'node:module'
import {test} from 'node:test'
import {planDraft,planPayload} from '../src/schedule-ui.js'
const require=createRequire(new URL('../package.json',import.meta.url))
const {parse,compileScript,compileTemplate}=require('@vue/compiler-sfc')
const vue=require('vue')
function component(name,props){
  const {descriptor}=parse(fs.readFileSync(new URL(`../src/components/${name}.vue`,import.meta.url),'utf8'))
  const script=compileScript(descriptor,{id:name})
  assert.deepEqual(compileTemplate({id:name,source:descriptor.template.content,compilerOptions:{bindingMetadata:script.bindings}}).errors,[])
  const env={...vue,TimePointPicker:{}}
  const definition=new Function(...Object.keys(env),script.content.replace(/^import .*$/gm,'').replace('export default','return'))(...Object.values(env))
  const values=[]
  const state=definition.setup(props,{expose:()=>{},emit:(event,value)=>values.push(value)})
  return {state,values}
}
test('clickable times change hour/minute independently and allow multiple slots without typed syntax',()=>{
  const {state,values}=component('TimePointPicker',{modelValue:'09:15, 18:30'})
  state.change(1,0,'20');assert.equal(values.pop(),'09:15, 20:30')
  state.change(0,1,'45');assert.equal(values.pop(),'09:45, 18:30')
  state.remove(0);assert.equal(values.pop(),'18:30')
  state.add();assert.equal(values.pop(),'09:15, 18:30, 00:00')
})
test('calendar dates and weekdays are selected without losing other draft fields',()=>{
  const draft=planDraft({kind:'weekly',times:['08:30','18:45'],weekdays:[0,4],dates:['2026-10-18']})
  const {state,values}=component('CalendarPlanFields',{modelValue:draft})
  state.weekday(2);assert.deepEqual(values.pop().weekdays,[0,2,4])
  state.selectedDate.value='2026-10-20';state.addDate()
  const next=values.pop();assert.equal(next.dates,'2026-10-18\n2026-10-20');assert.equal(next.times,draft.times)
  assert.deepEqual(planPayload(next).times,['08:30','18:45'])
  assert.throws(()=>planPayload({...next,times:''}))
  assert.throws(()=>planPayload({...next,kind:'dates',dates:''}))
})
test('interval selector retains existing nonpreset values and enforces minimum on unit change',()=>{
  const {state,values}=component('IntervalPicker',{modelValue:90,min:60,max:86400})
  assert.equal(state.unit.value,1)
  assert.ok(state.values.value.includes(90))
  state.changeUnit({target:{value:'3600'}});assert.equal(values.pop(),3600)
  state.changeUnit({target:{value:'1'}});assert.equal(values.pop(),60)
})
