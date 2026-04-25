import { Component, type ErrorInfo, type ReactNode } from 'react';

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
  message: string;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = {
    hasError: false,
    message: '',
  };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return {
      hasError: true,
      message: error.message || '未知渲染错误',
    };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('[desktop-client][error-boundary] render failure', error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="dcc-shell">
          <main className="dcc-main" style={{ minHeight: '100vh' }}>
            <section className="dcc-content" style={{ display: 'flex', alignItems: 'center' }}>
              <div className="dcc-error">
                <strong>界面渲染失败。</strong>
                <p className="dcc-paragraph">{this.state.message}</p>
                <p className="dcc-paragraph">请在底层数据问题修复后重新加载桌面客户端。</p>
              </div>
            </section>
          </main>
        </div>
      );
    }

    return this.props.children;
  }
}
