import {defineConfig} from 'vite';
export default defineConfig({server:{host:'127.0.0.1',port:1429,strictPort:true,proxy:{'/api':{target:'http://127.0.0.1:1449',rewrite:()=>'/api'}}}});
