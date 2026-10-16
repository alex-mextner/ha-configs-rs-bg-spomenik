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
    this._clipBusy = false;
    const clipboard = this.querySelector('[data-clip-text]');
    if (clipboard) clipboard.value = '';
    clearInterval(this._timer);
    this._timer = undefined;
  }
  async authorize() {
    const {session} = await this._hass.callWS({type: "ha-ingress/session"});
    await this._hass.callApi("POST", "chatgpt_native/session", {session});
  }
  bindClipboard(current) {
    const drawer = this.querySelector('[data-clipboard]');
    this.querySelector('[data-clipboard-toggle]').onclick = () => {
      drawer.hidden = !drawer.hidden;
      this.querySelector('[data-clip-text]').value = '';
      this.querySelector('[data-clip-status]').textContent = 'Передаётся только текст, до 64 КБ.';
    };
    for (const button of this.querySelectorAll('[data-clip-action]')) {
      button.onclick = async () => {
        if (this._clipBusy) return;
        this._clipBusy = true;
        try { await this.clipboardAction(button.dataset.clipAction, current); }
        finally { this._clipBusy = false; }
      };
    }
  }
  async clipboardAction(action, current) {
    const area = this.querySelector('[data-clip-text]');
    const status = this.querySelector('[data-clip-status]');
    const alive = () => current() && this.querySelector('[data-clip-text]') === area;
    try {
      if (action === 'local') {
        if (!navigator.clipboard?.readText) throw new Error('manual');
        const text = await navigator.clipboard.readText();
        if (!alive()) return;
        area.value = text;
        status.textContent = 'Текст получен с устройства. Нажмите «На Ubuntu». ';
      } else if (action === 'send') {
        if (new TextEncoder().encode(area.value).length > 65536) throw new Error('size');
        await this._hass.callApi('POST', 'chatgpt_native/clipboard', {text: area.value});
        if (alive()) status.textContent = 'Текст в буфере Ubuntu. Вставьте его в нужное приложение: Ctrl+V.';
      } else if (action === 'receive') {
        const result = await this._hass.callApi('GET', 'chatgpt_native/clipboard');
        if (!alive()) return;
        area.value = result.text;
        status.textContent = 'Текст получен с Ubuntu. Нажмите «Копировать на устройство». ';
      } else if (action === 'copy') {
        if (!navigator.clipboard?.writeText) {
          area.focus(); area.select(); throw new Error('manual');
        }
        await navigator.clipboard.writeText(area.value);
        if (alive()) status.textContent = 'Скопировано в буфер этого устройства.';
      }
    } catch (error) {
      if (!alive()) return;
      const code = error.status_code ?? error.status;
      if (code === 423) {
        area.value = '';
        status.textContent = 'Сначала разблокируйте Ubuntu на экране ниже. Буфер заблокирован вместе с экраном.';
      } else if (error.message === 'size') {
        status.textContent = 'Слишком большой текст: максимум 64 КБ.';
      } else if (action === 'copy' || action === 'local') {
        status.textContent = 'Браузер не дал доступ к буферу. Скопируйте или вставьте текст в это поле вручную.';
      } else {
        status.textContent = 'Буфер недоступен. Проверьте, что Ubuntu разблокирована и в буфере есть текст, затем повторите.';
      }
    }
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
        ? 'Основной экран Ubuntu. При блокировке введите пароль Ubuntu.'
        : 'ChatGPT на основном экране Ubuntu.';
      this.innerHTML = '<div style="height:64px;box-sizing:border-box;padding:6px 8px;background:var(--card-background-color);font:14px sans-serif;display:flex;align-items:center;gap:12px"><span style="flex:1">' + notice + '</span><button type="button" data-clipboard-toggle>📋 Буфер обмена</button></div>'
        + '<section data-clipboard hidden style="position:absolute;z-index:10;top:64px;left:8px;right:8px;padding:12px;border:1px solid var(--divider-color);background:var(--card-background-color,#fff);color:var(--primary-text-color,#111);box-shadow:0 4px 12px #0004;font:14px sans-serif"><label>Текст между устройством и Ubuntu<textarea data-clip-text rows="5" spellcheck="false" style="display:block;box-sizing:border-box;width:100%;margin:8px 0;font:16px monospace" placeholder="Вставьте текст сюда обычной командой «Вставить»"></textarea></label><div style="display:flex;gap:8px;flex-wrap:wrap"><button type="button" data-clip-action="local">Вставить с устройства</button><button type="button" data-clip-action="send">На Ubuntu →</button><button type="button" data-clip-action="receive">← С Ubuntu</button><button type="button" data-clip-action="copy">Копировать на устройство</button></div><p data-clip-status role="status" aria-live="polite">Передаётся только текст, до 64 КБ. Файлы не передаются.</p></section>'
        + '<iframe title="' + (this.desktopMode ? 'Ubuntu Desktop' : 'Native ChatGPT') + '" style="border:0;width:100%;height:calc(100% - 64px);position:absolute;inset:64px 0 0 0" allow="clipboard-read; clipboard-write"></iframe>';
      this.bindClipboard(current);
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
