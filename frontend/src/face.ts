import {api} from './types';
export interface FaceResult { done:boolean; face_token?:string; bound?:boolean }
export async function runFace(start:()=>Promise<unknown>):Promise<FaceResult>{
 if(!document.querySelector('link[data-face-style]')){const link=document.createElement('link');link.rel='stylesheet';link.href='/face-camera.css?v=20261019-guide3';link.dataset.faceStyle='1';document.head.append(link);}
 const url='/face-camera.js?v=20261019-guide3';const module=await import(/* @vite-ignore */ url);
 return module.runFace(start);
}
export function faceStatus(){return api<{enabled?:boolean;server_enabled?:boolean;user_enabled?:boolean;bound:boolean;ready:boolean;updated_at?:string}>('/face/status');}
