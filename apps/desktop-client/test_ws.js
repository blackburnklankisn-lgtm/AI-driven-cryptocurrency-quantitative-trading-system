import WebSocket from 'ws';

const ws = new WebSocket('ws://localhost:8000/api/v1/ws/logs', {
  headers: {
    Origin: 'http://localhost:5173'
  }
});

ws.on('open', () => {
  console.log('Connected!');
  ws.close();
});

ws.on('error', (err) => {
  console.error('Error:', err.message);
});

ws.on('unexpected-response', (req, res) => {
    console.error(`Unexpected server response: ${res.statusCode} ${res.statusMessage}`);
});
