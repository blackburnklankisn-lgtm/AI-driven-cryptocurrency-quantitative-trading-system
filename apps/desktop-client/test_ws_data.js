import WebSocket from 'ws';

const ws = new WebSocket('ws://localhost:5173/api/v1/ws/status', {
  headers: {
    Origin: 'http://localhost:5173'
  }
});

let timeout = setTimeout(() => {
  console.log('Timeout waiting for message');
  ws.close();
}, 5000);

ws.on('open', () => {
  console.log('Connected! Waiting for message...');
});

ws.on('message', (data) => {
  console.log('Received:', data.toString());
  clearTimeout(timeout);
  ws.close();
});

ws.on('error', (err) => {
  console.error('Error:', err.message);
});

ws.on('close', () => {
    console.log('Closed.');
});
