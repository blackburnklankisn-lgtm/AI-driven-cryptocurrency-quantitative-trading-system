import type { ReactNode } from 'react';

interface MetricCardProps {
  label: string;
  value: string;
  accent?: 'bull' | 'bear' | 'risk' | 'info' | 'neutral';
  subtitle?: string;
  icon?: ReactNode;
}

export function MetricCard({ label, value, accent = 'neutral', subtitle, icon }: MetricCardProps) {
  return (
    <section className={`dcc-card dcc-card--${accent}`}>
      <header className="dcc-card__header">
        <span className="dcc-card__label">{label}</span>
        {icon ? <span className="dcc-card__icon">{icon}</span> : null}
      </header>
      <div className="dcc-card__value">{value}</div>
      {subtitle ? <p className="dcc-card__subtitle">{subtitle}</p> : null}
    </section>
  );
}