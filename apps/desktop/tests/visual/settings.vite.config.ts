import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";
export default defineConfig({resolve:{alias:[{find:/^\.\/bridge$/,replacement:fileURLToPath(new URL("./settings-bridge.ts",import.meta.url))}]},
  server:{host:"127.0.0.1",port:1421,strictPort:true,proxy:{"/api":"http://127.0.0.1:1422"}}});
