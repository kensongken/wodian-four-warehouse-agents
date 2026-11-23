// Private in-memory projections: no user DB, credentials or outbound jobs copied.
export function comparisonProjection(state,{withoutOwner=false,customerId=null}={}){
 const studio=structuredClone(state.personaStudio),library=structuredClone(state.knowledgeLibrary||{revision:0,items:[],imports:[]});
 library.imports=[];
 if(withoutOwner){studio.profile.examples=[];studio.profile.phrases='';studio.profile.source='base_tone_only';studio.trials=[];studio.feedbackRevision=0;library.items=library.items.filter(k=>k.kind==='faq');}
 return{settings:{},users:[],sessions:[],customers:customerId?state.customers.filter(c=>c.id===customerId&&!c.deletedAt).map(({id,version,assigneeId,communicationPreferences,communicationPreferenceRevision})=>({id,version,assigneeId,communicationPreferences,communicationPreferenceRevision})):[],messages:[],drafts:[],tasks:[],reminders:[],materials:[],learning:[],personas:[],audit:[],personaStudio:studio,knowledgeLibrary:library,coachLab:withoutOwner?{lessons:[],lessonRevision:0}:structuredClone({lessons:state.coachLab?.lessons||[],lessonRevision:state.coachLab?.lessonRevision||0})};
}
export function comparisonMemoryStore(initial){let state=structuredClone(initial);return{read(){return structuredClone(state);},transact(fn){const next=structuredClone(state),result=fn(next);state=next;return result;}};}
export function ownerEvidence(trial){return{voiceExamples:trial.styleSamplesUsed?.length||0,feedback:trial.feedbackUsed?.length||0,serviceMethods:(trial.serviceReferences?.length||0)+(trial.knowledgeReferences?.filter(k=>k.kind==='experience').length||0)};}
