import {randomUUID} from 'node:crypto';
export class EditRetry{
 #guide;#assertContext;#clock;#permit=null;
 constructor({guide,assertContext,clock=Date.now}){this.#guide=guide;this.#assertContext=assertContext;this.#clock=clock;}
 issue(){this.#assertContext();this.#permit={token:randomUUID(),revision:this.#guide.preview().revision,expiresAt:this.#clock()+300000};return{token:this.#permit.token,expiresAt:this.#permit.expiresAt};}
 consume(token){this.#assertContext();const p=this.#permit;if(!p||token!==p.token||p.expiresAt<=this.#clock()||p.revision!==this.#guide.preview().revision)throw Error('Stale edit retry');this.#permit=null;}
 clear(){this.#permit=null;}
}
