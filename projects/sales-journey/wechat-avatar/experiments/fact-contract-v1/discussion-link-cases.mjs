export const LINK_GATES={correctRequired:8,total:8,falseInheritance:0,scope:'Relation subtask only. No runtime integration or owner resemblance certification.'};
export const LINK_CASES=[
 {id:'design-followup',previousAction:'compare_detail',previousCustomer:'给我挑个有设计感的吊坠。',message:'照你刚才的思路，会不会太显眼了？',expected:'followup'},
 {id:'outline-followup',previousAction:'compare_outline',previousCustomer:'想挑日常戴的耳钉。',message:'你说的柔和一点，是不是圆一点的意思？',expected:'followup'},
 {id:'repair-followup',previousAction:'repair_receipt',previousCustomer:'耳堵丢了，小票也找到了。',message:'那接下来要怎么确认能不能配上？',expected:'followup'},
 {id:'stop',previousAction:'compare_detail',previousCustomer:'想看看手链。',message:'刚才那个先不聊了，我今天不买了。',expected:'new_topic'},
 {id:'wearer-change',previousAction:'gift_habits',previousCustomer:'想送妈妈一对耳环。',message:'先不选她的，我给自己看看戒指。',expected:'new_topic'},
 {id:'privacy-chat',previousAction:'compare_daily',previousCustomer:'我想挑项链。',message:'先不说这些，今天家里有点事，心里堵得慌。',expected:'new_topic'},
 {id:'service-to-shopping',previousAction:'repair_limits',previousCustomer:'旧手链扣子坏了想修。',message:'旧的先放一边，我想选一条新的。',expected:'new_topic'},
 {id:'ambiguous',previousAction:'compare_detail',previousCustomer:'看看有细节的款式。',message:'算了，那个吧。',expected:'unclear'}
];
