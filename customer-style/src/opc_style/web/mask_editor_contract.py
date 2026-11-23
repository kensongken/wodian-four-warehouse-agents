"""DOM-free JavaScript contract for deterministic local mask editing."""

MASK_EDITOR_CONTRACT_SCRIPT = r"""
/* mask-editor-contract:start */
function maskEditorAssertMask(mask,width,height){
  if(!(mask instanceof Uint8Array)||!Number.isInteger(width)||!Number.isInteger(height)||width<=0||height<=0||mask.length!==width*height)throw Error('mask_editor_invalid');
}
function maskEditorFloodErase(mask,width,height,startX,startY){
  maskEditorAssertMask(mask,width,height);
  const result=mask.slice(),x=Math.floor(startX),y=Math.floor(startY);
  if(x<0||y<0||x>=width||y>=height||result[y*width+x]===0)return{mask:result,changed:false};
  const queueX=[x],queueY=[y];let cursor=0;result[y*width+x]=0;
  while(cursor<queueX.length){
    const currentX=queueX[cursor],currentY=queueY[cursor];cursor++;
    for(let offsetY=-1;offsetY<=1;offsetY++)for(let offsetX=-1;offsetX<=1;offsetX++){
      if(offsetX===0&&offsetY===0)continue;
      const nextX=currentX+offsetX,nextY=currentY+offsetY;
      if(nextX<0||nextY<0||nextX>=width||nextY>=height)continue;
      const index=nextY*width+nextX;
      if(result[index]!==0){result[index]=0;queueX.push(nextX);queueY.push(nextY)}
    }
  }
  return{mask:result,changed:true};
}
function maskEditorBrush(mask,width,height,fromPoint,toPoint,radius,value){
  maskEditorAssertMask(mask,width,height);
  if(!fromPoint||!toPoint||!Number.isFinite(radius)||radius<0)throw Error('mask_editor_invalid');
  const result=mask.slice(),paint=value>=128?255:0;
  const deltaX=toPoint.x-fromPoint.x,deltaY=toPoint.y-fromPoint.y,steps=Math.max(1,Math.ceil(Math.max(Math.abs(deltaX),Math.abs(deltaY))));
  let changed=false;
  for(let step=0;step<=steps;step++){
    const centerX=Math.round(fromPoint.x+deltaX*step/steps),centerY=Math.round(fromPoint.y+deltaY*step/steps);
    const minimumX=Math.floor(centerX-radius),maximumX=Math.ceil(centerX+radius),minimumY=Math.floor(centerY-radius),maximumY=Math.ceil(centerY+radius);
    for(let y=minimumY;y<=maximumY;y++)for(let x=minimumX;x<=maximumX;x++){
      if(x<0||y<0||x>=width||y>=height)continue;
      if((x-centerX)*(x-centerX)+(y-centerY)*(y-centerY)>radius*radius)continue;
      const index=y*width+x;if(result[index]!==paint){result[index]=paint;changed=true}
    }
  }
  return{mask:result,changed};
}
function maskEditorSourcePoint(clientX,clientY,rect,bitmapWidth,bitmapHeight,view={}){
  const zoom=Number.isFinite(view.zoom)&&view.zoom>0?view.zoom:1,panX=Number.isFinite(view.panX)?view.panX:0,panY=Number.isFinite(view.panY)?view.panY:0;
  if(!rect||rect.width<=0||rect.height<=0||bitmapWidth<=0||bitmapHeight<=0)return null;
  const localX=(clientX-rect.left-panX)/zoom,localY=(clientY-rect.top-panY)/zoom;
  if(localX<0||localY<0||localX>=rect.width||localY>=rect.height)return null;
  return{x:Math.floor(localX*bitmapWidth/rect.width),y:Math.floor(localY*bitmapHeight/rect.height)};
}
function maskEditorMetrics(baseMask,resultMask,width,height){
  maskEditorAssertMask(baseMask,width,height);maskEditorAssertMask(resultMask,width,height);
  let basePixelCount=0,resultPixelCount=0,addedPixelCount=0,deletedPixelCount=0,touchesEdge=false;
  for(let index=0;index<resultMask.length;index++){
    const before=baseMask[index]>=128,after=resultMask[index]>=128;
    if(before)basePixelCount++;if(after)resultPixelCount++;
    if(!before&&after)addedPixelCount++;if(before&&!after)deletedPixelCount++;
    if(after){const x=index%width,y=Math.floor(index/width);if(x===0||y===0||x===width-1||y===height-1)touchesEdge=true}
  }
  const visited=new Uint8Array(resultMask.length);let componentCount=0;
  for(let index=0;index<resultMask.length;index++){
    if(resultMask[index]<128||visited[index])continue;componentCount++;visited[index]=1;
    const queue=[index];let cursor=0;
    while(cursor<queue.length){
      const current=queue[cursor++],currentX=current%width,currentY=Math.floor(current/width);
      for(let offsetY=-1;offsetY<=1;offsetY++)for(let offsetX=-1;offsetX<=1;offsetX++){
        if(offsetX===0&&offsetY===0)continue;
        const x=currentX+offsetX,y=currentY+offsetY;if(x<0||y<0||x>=width||y>=height)continue;
        const next=y*width+x;if(resultMask[next]>=128&&!visited[next]){visited[next]=1;queue.push(next)}
      }
    }
  }
  const changedPixelCount=addedPixelCount+deletedPixelCount,changeRatio=changedPixelCount/Math.max(1,basePixelCount);
  return{basePixelCount,resultPixelCount,addedPixelCount,deletedPixelCount,changedPixelCount,changeRatio,componentCount,touchesEdge,empty:resultPixelCount===0,full:resultPixelCount===resultMask.length,unchanged:changedPixelCount===0,largeChangeWarning:changeRatio>0.15};
}
function createMaskEditorHistory(initialMask,maxSteps=50){
  if(!(initialMask instanceof Uint8Array)||!Number.isInteger(maxSteps)||maxSteps<1)throw Error('mask_editor_invalid');
  const initial=initialMask.slice();let snapshots=[initial.slice()],cursor=0;
  const same=(left,right)=>left.length===right.length&&left.every((value,index)=>value===right[index]);
  const current=()=>snapshots[cursor].slice();
  const push=mask=>{if(!(mask instanceof Uint8Array)||mask.length!==initial.length)throw Error('mask_editor_invalid');if(same(snapshots[cursor],mask))return false;snapshots=snapshots.slice(0,cursor+1);snapshots.push(mask.slice());if(snapshots.length>maxSteps+1)snapshots.shift();cursor=snapshots.length-1;return true};
  return{current,push,canUndo:()=>cursor>0,canRedo:()=>cursor<snapshots.length-1,undo:()=>{if(cursor>0)cursor--;return current()},redo:()=>{if(cursor<snapshots.length-1)cursor++;return current()},reset:()=>push(initial)};
}
function maskEditorSaveGate(metrics,busy,allowUnchanged=false){if(busy)return{canSave:false,message:'正在保存，请勿重复提交'};if(metrics.empty)return{canSave:false,message:'遮罩不能为空'};if(metrics.full)return{canSave:false,message:'遮罩不能覆盖整张图'};if(metrics.unchanged&&!allowUnchanged)return{canSave:false,message:'尚未修改遮罩'};if(metrics.touchesEdge)return{canSave:false,message:'遮罩碰到画布边缘，请继续清理'};if(metrics.unchanged)return{canSave:true,message:'机器轮廓未修改，可直接确认为整体金标'};if(metrics.largeChangeWarning)return{canSave:true,message:'修改超过 15%，请重点检查珠子、扣头和吊坠'};return{canSave:true,message:'有未保存修改'}}
function maskEditorLayerLabel(name){return name==='overall'?'整体商品':name==='clasp'?'搭扣':name==='pendant'?'吊坠':name}
function createMaskEditorLayerSet(overallMask,componentNames=[],maxSteps=50,componentMasks={}){
  if(!(overallMask instanceof Uint8Array)||!Array.isArray(componentNames)||componentNames.some(name=>typeof name!=='string'||!name.trim()))throw Error('mask_editor_invalid');
  const names=['overall',...componentNames.map(name=>name.trim())];
  if(new Set(names).size!==names.length)throw Error('mask_editor_invalid');
  let activeName='overall',layers={},overallFoundation=overallMask.slice();
  const makeLayer=(name,baseMask)=>({name,baseMask:baseMask.slice(),mask:baseMask.slice(),history:createMaskEditorHistory(baseMask,maxSteps),counts:{magic_wand_count:0,erase_stroke_count:0,restore_stroke_count:0}});
  const initialize=()=>{overallFoundation=overallMask.slice();layers={overall:makeLayer('overall',overallMask)};for(const name of names.slice(1)){const saved=componentMasks[name];layers[name]=makeLayer(name,saved instanceof Uint8Array&&saved.length===overallMask.length?saved:new Uint8Array(overallMask.length))}};
  initialize();
  const current=()=>layers[activeName];
  const get=name=>layers[name]||null;
  const select=name=>{if(!layers[name])throw Error('mask_editor_invalid');activeName=name;return current()};
  const commit=(mask,operation)=>{const layer=current();if(!layer.history.push(mask))return false;layer.mask=layer.history.current();if(activeName==='overall')overallFoundation=layer.mask.slice();if(operation)layer.counts[operation]++;return true};
  const undo=()=>{const layer=current();layer.mask=layer.history.undo();if(activeName==='overall')overallFoundation=layer.mask.slice();return layer.mask.slice()};
  const redo=()=>{const layer=current();layer.mask=layer.history.redo();if(activeName==='overall')overallFoundation=layer.mask.slice();return layer.mask.slice()};
  const reset=()=>{const layer=current();const changed=layer.history.reset();layer.mask=layer.history.current();if(activeName==='overall')overallFoundation=layer.mask.slice();return changed};
  const includeComponentsInOverall=()=>{
    const overall=layers.overall,merged=overallFoundation.slice();
    for(const name of names.slice(1))for(let index=0;index<merged.length;index++)if(layers[name].mask[index]>=128)merged[index]=255;
    const changed=merged.some((value,index)=>value!==overall.mask[index]);if(!changed)return false;
    overall.history.push(merged);overall.mask=overall.history.current();return true;
  };
  const snapshot=()=>({activeLayerName:activeName,overallFoundation:Array.from(overallFoundation),layers:names.map(name=>({name,mask:Array.from(layers[name].mask),counts:{...layers[name].counts}}))});
  const restore=draft=>{
    if(!draft||!Array.isArray(draft.layers)||draft.layers.length!==names.length||draft.layers.some((item,index)=>item.name!==names[index]||!Array.isArray(item.mask)||item.mask.length!==overallMask.length))return false;
    initialize();
    if(Array.isArray(draft.overallFoundation)&&draft.overallFoundation.length===overallMask.length)overallFoundation=Uint8Array.from(draft.overallFoundation,value=>value>=128?255:0);
    for(const item of draft.layers){const layer=layers[item.name],restored=Uint8Array.from(item.mask,value=>value>=128?255:0);layer.history.push(restored);layer.mask=layer.history.current();for(const key of Object.keys(layer.counts)){const value=item.counts&&item.counts[key];layer.counts[key]=Number.isInteger(value)&&value>=0?value:0}}
    activeName=layers[draft.activeLayerName]?draft.activeLayerName:'overall';return true;
  };
  return{names:()=>names.slice(),current,get,select,commit,undo,redo,reset,includeComponentsInOverall,snapshot,restore,activeName:()=>activeName};
}
function maskEditorMaskInside(innerMask,outerMask){
  if(!(innerMask instanceof Uint8Array)||!(outerMask instanceof Uint8Array)||innerMask.length!==outerMask.length)throw Error('mask_editor_invalid');
  return innerMask.every((value,index)=>value<128||outerMask[index]>=128);
}
function maskEditorLayerSaveGate(layerSet,width,height,busy,allowOverallUnchanged=false){
  if(busy)return{canSave:false,layerName:layerSet.activeName(),message:'正在保存，请勿重复提交'};
  const overall=layerSet.get('overall'),overallMetrics=maskEditorMetrics(overall.baseMask,overall.mask,width,height),overallGate=maskEditorSaveGate(overallMetrics,false,allowOverallUnchanged);
  if(!overallGate.canSave)return{canSave:false,layerName:'overall',message:overallGate.message};
  for(const name of layerSet.names().slice(1)){
    const layer=layerSet.get(name),label=maskEditorLayerLabel(name),metrics=maskEditorMetrics(layer.baseMask,layer.mask,width,height);
    if(metrics.empty)return{canSave:false,layerName:name,message:`请完成${label}图层`};
    if(!maskEditorMaskInside(layer.mask,overall.mask))return{canSave:false,layerName:name,message:`${label}不能超出整体商品边界`};
    const gate=maskEditorSaveGate(metrics,false,true);if(!gate.canSave)return{canSave:false,layerName:name,message:`${label}：${gate.message}`};
  }
  return{canSave:true,layerName:'overall',message:'所有金标图层可以保存'};
}
function maskEditorCrc32(parts){let crc=0xffffffff;for(const bytes of parts)for(const value of bytes){crc^=value;for(let bit=0;bit<8;bit++)crc=(crc>>>1)^((crc&1)?0xedb88320:0)}return(crc^0xffffffff)>>>0}
function maskEditorPngChunk(type,data){const typeBytes=new TextEncoder().encode(type),output=new Uint8Array(12+data.length),view=new DataView(output.buffer);view.setUint32(0,data.length);output.set(typeBytes,4);output.set(data,8);view.setUint32(8+data.length,maskEditorCrc32([typeBytes,data]));return output}
async function maskEditorMaskPngBlob(mask,width,height){maskEditorAssertMask(mask,width,height);const raw=new Uint8Array(height*(width+1));for(let y=0;y<height;y++){raw[y*(width+1)]=0;raw.set(mask.subarray(y*width,(y+1)*width),y*(width+1)+1)}const compressed=new Uint8Array(await new Response(new Blob([raw]).stream().pipeThrough(new CompressionStream('deflate'))).arrayBuffer()),header=new Uint8Array(13),view=new DataView(header.buffer);view.setUint32(0,width);view.setUint32(4,height);header.set([8,0,0,0,0],8);return new Blob([new Uint8Array([137,80,78,71,13,10,26,10]),maskEditorPngChunk('IHDR',header),maskEditorPngChunk('IDAT',compressed),maskEditorPngChunk('IEND',new Uint8Array())],{type:'image/png'})}
async function maskEditorSha256(bytes){const digest=await crypto.subtle.digest('SHA-256',bytes);return[...new Uint8Array(digest)].map(value=>value.toString(16).padStart(2,'0')).join('')}
async function maskEditorManifestSha256(payload){const ordered={};Object.keys(payload).sort().forEach(key=>ordered[key]=payload[key]);return maskEditorSha256(new TextEncoder().encode(JSON.stringify(ordered)))}
/* mask-editor-contract:end */
""".strip()
