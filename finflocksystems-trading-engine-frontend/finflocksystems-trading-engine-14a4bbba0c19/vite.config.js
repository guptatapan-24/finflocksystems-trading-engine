import { loadEnv } from 'vite';
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
function normalizeBase(value) {
    var leading = value.startsWith('/') ? value : "/".concat(value);
    return "".concat(leading.replace(/\/+$/, ''), "/");
}
export default defineConfig(function (_a) {
    var mode = _a.mode;
    var env = loadEnv(mode, process.cwd(), '');
    var defaultBase = mode === 'production' ? '/qfsplatform/trading_engine_frontend/' : '/';
    var configuredBase = process.env.VITE_APP_BASE_PATH || env.VITE_APP_BASE_PATH || defaultBase;
    var base = mode === 'test' ? '/' : normalizeBase(configuredBase);
    return {
        base: base,
        plugins: [react()],
        server: {
            proxy: {
                '/api/v1': {
                    target: 'http://localhost:8000',
                    changeOrigin: true,
                },
            },
        },
        test: { environment: 'jsdom', globals: true, setupFiles: './tests/setup.ts' },
    };
});
