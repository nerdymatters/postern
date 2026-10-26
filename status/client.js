export const client = `
const labels = {operational:'Operational',degraded:'Degraded',outage:'Unavailable',unknown:'Unknown'};
const headings = {operational:'All monitored services are operational',degraded:'Some services need attention',outage:'A service is unavailable',unknown:'Some checks are awaiting an update'};
const summary=document.getElementById('summary'), updated=document.getElementById('updated');
function text(tag,value,className){const element=document.createElement(tag);element.textContent=value;if(className)element.className=className;return element;}
function when(value){return value ? new Date(value).toLocaleString() : 'No reading received';}
function render(data){
 summary.textContent=headings[data.overall]||headings.unknown;
 updated.textContent='Status retrieved '+when(data.generated_at)+'.';
 const container=document.getElementById('services');container.replaceChildren();
 for(const group of [...new Set(data.services.map(s=>s.group))]){
  const section=document.createElement('section');section.className='group';section.append(text('h3',group));
  const rows=document.createElement('div');rows.className='rows';
  for(const service of data.services.filter(s=>s.group===group)){
   const row=document.createElement('div');row.className='row';
   row.append(text('span',service.name),text('span',labels[service.state]||labels.unknown,'state '+service.state),text('span','Checked: '+when(service.checked_at),'checked'));rows.append(row);
  }section.append(rows);container.append(section);
 }
 const changes=document.getElementById('changes');changes.replaceChildren();
 for(const change of data.changes){
  const service=data.services.find(s=>s.id===change.service);if(!service)continue;
  const li=text('li',service.name+' — '+(labels[change.state]||labels.unknown));li.append(text('time',when(change.changed_at)));changes.append(li);
 }
 if(!data.changes.length)changes.append(text('li','No recorded changes yet.'));
}
let previous=null,busy=false;
async function refresh(){
 if(busy)return;busy=true;
 try{
  const response=await fetch('/api/status',{cache:'no-store',signal:AbortSignal.timeout(12000)});
  if(!response.ok)throw new Error('Status unavailable');
  const data=await response.json();if(!Array.isArray(data.services)||!Array.isArray(data.changes))throw new Error('Invalid response');
  previous=data;render(data);
 }catch{
  if(previous)render({...previous,overall:'unknown',services:previous.services.map(s=>({...s,state:'unknown'}))});
  summary.textContent='Status updates are unavailable';updated.textContent='Service health cannot be confirmed. Retrying automatically.';
 }finally{busy=false;}
}
document.getElementById('refresh').addEventListener('click',refresh);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
refresh();setInterval(refresh,30000);
`
