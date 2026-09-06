/** Chronological DOM order, paired rows with traffic outside each pair. */
export function serpentineLayout(width: number, count: number) {
  const compact = width < 600
  const padding = compact ? 16 : 36
  const gap = compact ? 12 : 18
  const preferred = compact ? 128 : 170
  const columns = Math.max(1, Math.min(6, Math.floor((width-padding*2+gap)/(preferred+gap))))
  const cardWidth = Math.min(184,(width-padding*2-gap*(columns-1))/columns)
  const cardHeight = compact ? 158 : 168
  const left = (width-columns*cardWidth-(columns-1)*gap)/2
  const point = (index: number) => {
    const row=Math.floor(index/columns), offset=index%columns
    const column=row%2 ? columns-1-offset : offset
    return {x:left+column*(cardWidth+gap),y:48+row*(cardHeight+32)+Math.floor(row/2)*96,row,reverse:row%2===1}
  }
  const rowAt = (y: number, itemCount: number) => {
    let closest=0, distance=Infinity
    for(let row=0;row<=Math.floor(itemCount/columns);row++) {
      const delta=Math.abs(y-point(row*columns).y-cardHeight/2)
      if(delta<distance){closest=row;distance=delta}
    }
    return closest
  }
  const last=point(Math.max(0,count-1))
  return {width,columns,cardWidth,cardHeight,point,rowAt,height:count===0?100:last.y+cardHeight+(last.reverse?60:32)}
}

export function serpentineEdge(layout: ReturnType<typeof serpentineLayout>, index: number) {
  const from=layout.point(index), to=layout.point(index+1)
  const x1=from.x+layout.cardWidth/2, x2=to.x+layout.cardWidth/2
  const y1=from.y+(from.reverse?layout.cardHeight:0)
  const y2=to.y+(to.reverse?layout.cardHeight:0)
  const direction=from.reverse?1:-1
  const arrow={arrowX:x2,arrowY:y2,arrowDirection:to.reverse?-1:1}
  if(from.row===to.row) return {...arrow,
    path:`M ${x1} ${y1} C ${x1} ${y1+direction*48} ${x2} ${y2+direction*48} ${x2} ${y2}`,
    x:(x1+x2)/2,y:y1+direction*33,turn:false}
  const edge=from.reverse?7:layout.width-7
  const outside1=y1+direction*35, outside2=y2+(to.reverse?35:-35)
  return {...arrow,
    path:`M ${x1} ${y1} C ${x1} ${outside1} ${edge} ${outside1} ${edge} ${y1} L ${edge} ${y2} C ${edge} ${outside2} ${x2} ${outside2} ${x2} ${y2}`,
    x:from.reverse?56:layout.width-56,y:(from.y+layout.cardHeight+to.y)/2,turn:true}
}
