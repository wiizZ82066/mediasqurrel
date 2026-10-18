export function planDraft(plan={}) {
  const today=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date())
  return {kind:plan.kind||'daily',timezone:plan.timezone||'Asia/Shanghai',times:(plan.times||['09:00']).join(', '),start_date:plan.start_date||today,weekdays:[...(plan.weekdays||[0,1,2,3,4])],dates:(plan.dates||[today]).join('\n'),unit:plan.unit||'days',every:plan.every||1,count:plan.count||'',end_date:plan.end_date||'',enabled:true}
}
export function planPayload(draft) {
  const times=[...new Set(draft.times.split(/[,，\s]+/).filter(Boolean))]
  const dates=[...new Set(draft.dates.split(/[,，\s]+/).filter(Boolean))]
  if(!times.length)throw new Error('请至少添加一个执行时间')
  if(draft.kind==='weekly' && !draft.weekdays.length)throw new Error('请至少选择一个星期')
  if(draft.kind==='dates' && !dates.length)throw new Error('请至少添加一个日期')
  return {...draft,times,dates,count:draft.count?Number(draft.count):null,end_date:draft.end_date||null,every:Number(draft.every),enabled:true}
}
