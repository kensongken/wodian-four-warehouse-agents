// Change output grammar only; keep the prompt, parser and attribution gate unchanged.
const branch=(act,quote)=>({type:'object',additionalProperties:false,required:['act','quote'],properties:{act,quote}});
export const INTENT_SCHEMA_V2={oneOf:[
 branch({enum:['pause_sales','change_topic','continue_selection']},{type:'string',minLength:1,maxLength:800}),
 branch({const:'unclear'},{type:'null'})
]};
