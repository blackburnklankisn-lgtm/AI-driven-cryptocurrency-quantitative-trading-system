import type { ReactNode } from 'react';

interface SectionPanelProps {
  title: string;
  kicker?: string;
  children: ReactNode;
  actions?: ReactNode;
}

export function SectionPanel({ title, kicker, children, actions }: SectionPanelProps) {
  return (
    <section className="dcc-panel">
      <header className="dcc-panel__header">
        <div>
          {kicker ? <div className="dcc-panel__kicker">{kicker}</div> : null}
          <h2 className="dcc-panel__title">{title}</h2>
        </div>
        {actions ? <div className="dcc-panel__actions">{actions}</div> : null}
      </header>
      <div className="dcc-panel__body">{children}</div>
    </section>
  );
}