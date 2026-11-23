import {selectionFocus} from './conversation-understanding.mjs';
import {customerNeedState,customerNeedSummary} from './customer-needs.mjs';
// Service examples are generated methods explicitly approved by the owner, never owner speech.
export function validServiceLesson(state,lesson,now=Date.now(),seen=new Set()){
 if(seen.has(lesson.id))return false;const visited=new Set([...seen,lesson.id]);
 return lesson.status==='approved'&&lesson.profileId===state.personaStudio?.profile?.id&&(lesson.knowledgeReferences||[]).every(r=>state.knowledgeLibrary?.items.some(k=>k.id===r.id&&k.version===r.version&&k.status==='approved'&&(k.kind!=='faq'||k.expires>now)))&&(lesson.sourceLessonReferences||[]).every(r=>{const parent=state.coachLab?.lessons.find(l=>l.id===r.id&&l.version===r.version);return parent&&validServiceLesson(state,parent,now,visited);});
}
export function selectServiceLessons(state,key,now=Date.now(),needs={}){
 if(!key||['recap','update_preferences'].includes(key))return[];
 const wanted=selectionFocus(needs)?.key;
 const compatible=lesson=>{
  const statements=[...(lesson.context||[]),lesson.question].map(question=>({question}));
  const own=selectionFocus(customerNeedSummary(customerNeedState(statements)))?.key;
  return !wanted||!own||wanted===own;
 };
 return(state.coachLab?.lessons||[]).filter(l=>l.goal===key&&validServiceLesson(state,l,now)&&compatible(l)).slice(-1).filter(l=>Buffer.byteLength((l.context||[]).join('')+l.question+l.reply,'utf8')<=650).map(l=>({id:l.id,version:l.version,context:l.context||[],question:l.question,reply:l.reply,source:'owner_approved_generated_service_method',sourceModel:l.sourceModel}));
}
