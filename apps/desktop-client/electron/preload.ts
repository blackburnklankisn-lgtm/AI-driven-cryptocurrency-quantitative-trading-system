import { contextBridge } from 'electron';

contextBridge.exposeInMainWorld('electronAPI', {
  // Can expose IPC calls here if needed later.
  // For now, the desktop app just talks to FastAPI over localhost.
});
