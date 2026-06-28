import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react-swc";
import path from "node:path";
export default defineConfig(function (_a) {
    var _b, _c;
    var mode = _a.mode;
    var env = loadEnv(mode, process.cwd(), "");
    var backendUrl = (_c = (_b = env.VITE_FASTAPI_BACKEND_URL) !== null && _b !== void 0 ? _b : env.FASTAPI_BACKEND_URL) !== null && _c !== void 0 ? _c : "http://127.0.0.1:8000";
    return {
        plugins: [react()],
        resolve: {
            alias: {
                "@": path.resolve(__dirname, "./src"),
            },
        },
        server: {
            proxy: {
                "/api": {
                    target: backendUrl,
                    changeOrigin: true,
                    timeout: 600000,
                    proxyTimeout: 600000,
                },
            },
        },
    };
});
