import {defineConfig} from "vite";
export default defineConfig({server:{host:'127.0.0.1',port:1423,strictPort:true,proxy:{'/api':'http://127.0.0.1:1424'}}});
