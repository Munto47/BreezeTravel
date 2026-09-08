async function installMapObservation(page){
  // Observe the actual SDK operations, never substitute an SDK or an API result.
  await page.addInitScript(()=>{
    const records=[],objects=new WeakMap(),constructors=new WeakMap(),wrapped=new WeakMap()
    window.__lodgingMapObservation={records,snapshot(){return records.filter(r=>!r.destroyed&&r.container.closest('[data-testid="route-map"]')?.getBoundingClientRect().width>0).map(r=>({
      complete_events:r.completeEvents,last_complete_at:r.completeAt,last_fit_at:r.fitAt,fit_count:r.fitCount,
      zoom:r.instance.getZoom(),center:[r.instance.getCenter().getLng(),r.instance.getCenter().getLat()],
      fit_calls:r.fitCalls,
      active_paths:[...r.overlays].filter(o=>objects.get(o)?.kind==='Polyline').map(o=>objects.get(o).path),
      canvas_visible:[...r.container.querySelectorAll('canvas')].some(c=>c.width>0&&c.height>0&&c.getBoundingClientRect().width>0),
    }))}}
    function sdkProxy(sdk){
      if(!sdk||(typeof sdk!=='object'&&typeof sdk!=='function'))return sdk
      if(wrapped.has(sdk))return wrapped.get(sdk)
      const proxy=new Proxy(sdk,{get(target,key,receiver){
        const Original=Reflect.get(target,key,receiver)
        if(!['Map','Marker','Polyline'].includes(key)||typeof Original!=='function')return Original
        if(constructors.has(Original))return constructors.get(Original)
        const Observed=new Proxy(Original,{construct(C,args){
          const instance=Reflect.construct(C,args,C)
          if(key==='Map'&&args[0] instanceof HTMLElement){
            const record={instance,container:args[0],overlays:new Set(),completeEvents:0,completeAt:0,fitAt:0,fitCount:0,fitCalls:[],destroyed:false}
            records.push(record)
            instance.on('complete',()=>{record.completeEvents++;record.completeAt=performance.now()})
            for(const method of ['add','remove','setFitView','destroy']){
              const original=instance[method]
              instance[method]=function(...values){
                if(method==='add')for(const o of (Array.isArray(values[0])?values[0]:[values[0]]))record.overlays.add(o)
                if(method==='remove')for(const o of (Array.isArray(values[0])?values[0]:[values[0]]))record.overlays.delete(o)
                if(method==='setFitView'){
                  record.fitCount++;record.fitAt=performance.now()
                  const bounds=values[0].flatMap(o=>typeof o.getPath==='function'?o.getPath():typeof o.getPosition==='function'?[o.getPosition()]:[]).map(p=>[p.getLng(),p.getLat()])
                  record.fitCalls.push({at:performance.now(),immediately:values[1],overlay_count:values[0].length,
                    extent:bounds.length?[Math.min(...bounds.map(p=>p[0])),Math.min(...bounds.map(p=>p[1])),Math.max(...bounds.map(p=>p[0])),Math.max(...bounds.map(p=>p[1]))]:null})
                }
                if(method==='destroy')record.destroyed=true
                return original.apply(this,values)
              }
            }
          }else if(key==='Polyline')objects.set(instance,{kind:key,path:args[0]?.path})
          return instance
        }})
        constructors.set(Original,Observed)
        return Observed
      }})
      wrapped.set(sdk,proxy)
      return proxy
    }
    let value=window.AMap
    Object.defineProperty(window,'AMap',{configurable:true,enumerable:true,get:()=>sdkProxy(value),set:next=>{value=next}})
  })
}
function watchMapResources(page){
  const pending=new Set(),stats={started:0,finished:0,failed:0,last_activity:Date.now()}
  const relevant=request=>{const host=new URL(request.url()).hostname;return /(^|\.)(amap\.com|autonavi\.com|aoscdn\.com)$/.test(host)}
  page.on('request',request=>{if(relevant(request)){pending.add(request);stats.started++;stats.last_activity=Date.now()}})
  for(const event of ['requestfinished','requestfailed'])page.on(event,request=>{if(pending.delete(request)){
    stats[event==='requestfailed'?'failed':'finished']++;stats.last_activity=Date.now()
  }})
  return ()=>({...stats,pending:pending.size,idle_ms:Date.now()-stats.last_activity})
}

module.exports={installMapObservation,watchMapResources}
