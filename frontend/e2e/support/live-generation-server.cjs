const http = require('node:http')

// A local-only continuous SSE fixture. Controls advance one snapshot without
// closing the event connection, so rendering can be checked before completion.
function createLiveGenerationServer() {
  const ref = 'live-generation-cards-resource'
  const names = ['故宫博物院','景山公园','北海公园','什刹海','天坛公园','前门大街','大栅栏','天安门广场','颐和园','圆明园','北京动物园','景山公园']
  const connections = new Set(), events = [], requests = []
  let result = null, current = null, nextId = 0
  const unavailable = {status:'UNAVAILABLE',message:'尚未检查',days:[],available_actions:[]}
  function snapshot(count, ready, detail = false) {
    return {status:'PARTIAL_RESULT',ownership:'ANONYMOUS',is_demo:false,
      assumptions:[{key:'destination',label:'目的地',value:'北京',editable:true},{key:'calendar',label:'日序',value:'Day 1–3',editable:true},{key:'party_size',label:'人数',value:'2 人',editable:true}],
      coverage:{recognized_place_count:count,confirmed_place_count:ready,unresolved_place_count:count-ready,unclassified_mention_count:0,unprocessed_count:0,complete:count===ready},
      days:Array.from({length:Math.max(1,Math.ceil(count/4))},(_,day)=>({label:`Day ${day+1}`,activities:names.slice(day*4,Math.min(count,day*4+4)).map((name,n)=>{
        const index=day*4+n
        return {activity_token:`controlled-visit-${index}`,visit_id:`controlled-visit-${index}`,name,city:'北京',category:'景点',status:index<ready?'READY':'NEEDS_CONFIRMATION',verification_pending:index>=ready,area_or_address:'北京市',available_actions:['VIEW_DETAILS','REPLACE','DELETE','MOVE'],source_details:detail&&index===0?[{name:'太和殿',optional:false}]:[]}
      }),alternatives:day===0?[{activity_token:'controlled-optional',name:'南锣鼓巷',city:'北京',category:'景点',status:'NEEDS_CONFIRMATION',available_actions:['ADD_TO_DAY']}]:[],unprocessed_count:0,source_notes:day===0?[{note_id:'controlled-cancel',text:'已取消：王府井'}]:[]})),
      map:unavailable,stay:{...unavailable,area_summary:null,searched_scopes:[],candidates:[]},available_actions:['EDIT_ASSUMPTIONS','EDIT_CARDS']}
  }
  function send(type, payload, id = ++nextId) {
    const event={id,type,payload};events.push(event)
    for(const res of connections)res.write(`id: ${id}\nevent: ${type}\ndata: ${JSON.stringify(payload)}\n\n`)
  }
  function advance(count,ready,detail=false) {
    current={status:'PROCESSING',message:'正在整理',phase:'CHECKING_PLACES',event_cursor:nextId+1,retry_after_ms:500,
      progress:{day_count:Math.max(1,Math.ceil(count/4)),card_count:count,places_checked:ready,places_total:count,semantic_complete:false,places_total_final:false},snapshot:snapshot(count,ready,detail)}
    send('progress',current)
  }
  function finish(cancelled=false) {
    result=current?.snapshot || snapshot(0,0)
    result={...result,status:cancelled?'PARTIAL_RESULT':'READY',coverage:{...result.coverage,complete:!cancelled}}
    send('result_available',{...current,status:cancelled?'PARTIAL':'READY',message:'已保留行程'})
    for(const res of connections)res.end()
  }
  function reset() { for(const res of connections)res.end(); events.length=0;requests.length=0;result=null;current={status:'PROCESSING',message:'正在整理',phase:'RECEIVED',event_cursor:nextId,retry_after_ms:500,progress:{day_count:0,card_count:0,places_checked:0,places_total:0,semantic_complete:false,places_total_final:false},snapshot:null} }
  reset()
  const server=http.createServer(async(req,res)=>{
    const url=new URL(req.url,'http://localhost'), path=url.pathname
    const json=(body,status=200)=>{res.writeHead(status,{'content-type':'application/json',ETag:'tu3_controlled_generation'});res.end(JSON.stringify(body))}
    if(path==='/__control'){
      let body='';for await(const chunk of req)body+=chunk;const command=JSON.parse(body||'{}')
      if(command.action==='reset')reset()
      if(command.action==='advance')advance(command.count,command.ready,command.detail)
      if(command.action==='finish')finish()
      if(command.action==='disconnect')for(const stream of connections)stream.end()
      if(command.action==='duplicate'&&events.length){const event=events[events.length-1];send(event.type,event.payload,event.id)}
      return json({connections:connections.size,requests,cursor:nextId})
    }
    if(path==='/api/v3/trip-understandings'&&req.method==='POST')return json({public_resource_id:ref,status:'PROCESSING',message:'正在整理',result_url:'',events_url:''},202)
    if(!path.includes(ref))return json({},404)
    if(path.endsWith('/result'))return json(result||current,result?200:202)
    if(path.endsWith('/events')){
      const cursor=Number(req.headers['last-event-id']||0);requests.push(cursor)
      res.writeHead(200,{'content-type':'text/event-stream','cache-control':'no-cache, no-store, no-transform','x-accel-buffering':'no'});res.flushHeaders();res.write(': connected\n\n')
      for(const event of events)if(event.id>cursor)res.write(`id: ${event.id}\nevent: ${event.type}\ndata: ${JSON.stringify(event.payload)}\n\n`)
      connections.add(res);const heartbeat=setInterval(()=>res.write(': keepalive\n\n'),5000)
      res.on('close',()=>{clearInterval(heartbeat);connections.delete(res)})
      return
    }
    if(path.endsWith('/cancel')){finish(true);return json({status:'STOPPED_WITH_DRAFT',message:'当前结果已保留',has_editable_result:true})}
    if(path.endsWith('/map-renders/latest'))return json(unavailable)
    if(path.endsWith('/stay-suggestions'))return json({...unavailable,area_summary:null,searched_scopes:[],candidates:[]})
    if(path.endsWith('/supplementary'))return json({status:'UNAVAILABLE',days:[]})
    if(path.endsWith('/materialize'))return json({status:'READY',message:'',calendar:'Day 1–3',party_size:2,checks_available:true})
    if(path.endsWith('/checks'))return json({status:'STILL_NEEDS_CONFIRMATION',message:'',items:[],remaining_must_adjust:0,available_actions:[]})
    return json({},404)
  })
  return {server,advance,finish,reset,connections,requests,ref}
}
module.exports={createLiveGenerationServer}
if(require.main===module)createLiveGenerationServer().server.listen(Number(process.env.PORT||8183),'127.0.0.1')
