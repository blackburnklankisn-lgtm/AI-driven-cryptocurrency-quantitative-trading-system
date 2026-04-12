"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
const electron_1 = require("electron");
electron_1.contextBridge.exposeInMainWorld('electronAPI', {
// Can expose IPC calls here if needed later.
// For now, the desktop app just talks to FastAPI over localhost.
});
