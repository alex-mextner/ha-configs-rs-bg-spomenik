class NativeChatGPTPanel extends HTMLElement {
  constructor() {
    super();
    this._epoch = 0;
    this._starting = false;
  }
  get desktopMode() { return false; }
  set hass(value) {
    this._hass = value;
    this.start();
  }
  connectedCallback() { this.start(); }
  disconnectedCallback() {
    this._epoch += 1;
    this._starting = false;
    clearInterval(this._timer);
    this._timer = undefined;
  }
  async authorize() {
    const {session} = await this._hass.callWS({type: "ha-ingress/session"});
    await this._hass.callApi("POST", "chatgpt_native/session", {session});
  }
  async start() {
    if (!this.isConnected || !this._hass || this._starting) return;
    this._starting = true;
    const epoch = ++this._epoch;
    const current = () => this.isConnected && epoch === this._epoch;
    this.innerHTML = this.desktopMode
      ? '<p>Подключение к основному экрану Ubuntu…</p>'
      : '<p>Подключение к ChatGPT на основном экране Ubuntu…</p>';
    try {
      await this.authorize();
      if (!current()) return;
      const notice = this.desktopMode
        ? '<div style="height:64px;box-sizing:border-box;padding:8px;background:var(--card-background-color);font:14px sans-serif">Это основной экран Ubuntu: те же окна, что на мониторе. Управление общее.</div>'
        : '';
      const top = this.desktopMode ? '64px' : '0';
      this.innerHTML = notice + '<iframe title="' + (this.desktopMode ? 'Ubuntu Desktop' : 'Native ChatGPT') + '" style="border:0;width:100%;height:calc(100% - ' + top + ');position:absolute;inset:' + top + ' 0 0 0" allow="clipboard-read; clipboard-write"></iframe>';
      this.querySelector('iframe').src = '/api/chatgpt_native/vnc.html?autoconnect=true&reconnect=true&reconnect_delay=2000&resize=scale&path=/api/chatgpt_native/websockify';
      const timer = setInterval(() => {
        this.authorize().catch(() => {
          if (!current() || this._timer !== timer) return;
          clearInterval(timer);
          this.innerHTML = '<p>Сессия HA истекла. Войдите снова.</p>';
        });
      }, 120000);
      this._timer = timer;
    } catch (_) {
      if (!current()) return;
      this.innerHTML = '<p>Доступ разрешён только действующим администраторам HA.</p>';
    }
  }
}
customElements.define('chatgpt-native-panel', NativeChatGPTPanel);
class NativeUbuntuDesktopPanel extends NativeChatGPTPanel {
  get desktopMode() { return true; }
}
customElements.define('ubuntu-desktop-panel', NativeUbuntuDesktopPanel);
