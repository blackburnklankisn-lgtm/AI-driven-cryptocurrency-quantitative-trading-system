import { useEffect, useRef } from 'react';
import { createChart, CandlestickSeries, type IChartApi, type ISeriesApi, type CandlestickData } from 'lightweight-charts';
import { fetchWithEndpointRetry } from '../../services/backendEndpoint';

interface KlineChartProps {
  symbol?: string;
  height?: number;
}

export function KlineChart({ symbol = 'BTC/USDT', height = 320 }: KlineChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: { background: { color: 'transparent' }, textColor: '#8aa0bc' },
      grid: {
        vertLines: { color: 'rgba(90,118,153,0.12)' },
        horzLines: { color: 'rgba(90,118,153,0.12)' },
      },
      width: containerRef.current.clientWidth,
      height,
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#29c49a',
      downColor: '#ff7a6b',
      borderVisible: false,
      wickUpColor: '#29c49a',
      wickDownColor: '#ff7a6b',
    });

    chartRef.current = chart;
    seriesRef.current = series;

    const handleResize = () => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener('resize', handleResize);

    async function fetchKlines() {
      try {
        const res = await fetchWithEndpointRetry(`/api/v1/klines?symbol=${encodeURIComponent(symbol)}`);
        if (!res.ok) {
          console.warn('[KlineChart] klines response not ok', res.status);
          return;
        }
        const data = (await res.json()) as CandlestickData[];
        if (Array.isArray(data) && data.length > 0) {
          series.setData(data);
          chart.timeScale().fitContent();
          console.info('[KlineChart] loaded', data.length, 'bars for', symbol);
        }
      } catch (err) {
        console.error('[KlineChart] fetch failed', err);
      }
    }

    fetchKlines();
    const refreshTimer = window.setInterval(fetchKlines, 60_000);

    return () => {
      window.removeEventListener('resize', handleResize);
      window.clearInterval(refreshTimer);
      chart.remove();
    };
  }, [symbol, height]);

  return <div ref={containerRef} className="dcc-kline-chart" />;
}
