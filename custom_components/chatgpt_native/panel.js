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
  sendRemoteShortcut(letter) {
    if (!['c', 'v'].includes(letter)) throw new Error('unsupported remote shortcut');
    const frame = this.querySelector('iframe');
    let doc;
    try { doc = frame?.contentDocument; } catch (_) { throw new Error('remote desktop not connected'); }
    const canvas = doc?.querySelector('canvas');
    if (!canvas || !doc.documentElement.classList.contains('noVNC_connected') || doc.getElementById('noVNC_setting_view_only')?.checked) throw new Error('remote desktop not connected');
    const KeyboardEvent = frame.contentWindow.KeyboardEvent;
    canvas.focus();
    const emit = (type, key, code, ctrlKey) => canvas.dispatchEvent(new KeyboardEvent(type, {key, code, ctrlKey, bubbles:true, cancelable:true}));
    emit('keydown', 'Control', 'ControlLeft', true);
    try {
      try { emit('keydown', letter, `Key${letter.toUpperCase()}`, true); }
      finally { emit('keyup', letter, `Key${letter.toUpperCase()}`, true); }
    } finally { emit('keyup', 'Control', 'ControlLeft', false); }
  }
  bindClipboard(current) {
    const drawer = this.querySelector('[data-clipboard]');
    const shell = this.querySelector('[data-desktop-shell]');
    const toolbarToggle = this.querySelector('[data-toolbar-toggle]');
    toolbarToggle.onclick = () => {
      const collapsed = shell.dataset.toolbarCollapsed !== 'true';
      shell.dataset.toolbarCollapsed = String(collapsed);
      toolbarToggle.setAttribute('aria-expanded', String(!collapsed));
      toolbarToggle.innerHTML = collapsed ? NativeChatGPTPanel.tabler('chevron-down') : NativeChatGPTPanel.tabler('chevron-up');
      if (collapsed) { drawer.hidden = true; this.querySelector('[data-clip-text]').value = ''; }
    };
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
      if (action === 'ctrl-c' || action === 'ctrl-v') {
        if (!current()) return;
        this.sendRemoteShortcut(action.at(-1));
        if (alive()) status.textContent = `Ctrl+${action.at(-1).toUpperCase()} отправлено в Ubuntu.`;
      } else if (action === 'local') {
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
  static tabler(name) {
    const paths = {
      'chevron-up': '<path d="M6 15l6 -6l6 6"/>',
      'chevron-down': '<path d="M6 9l6 6l6 -6"/>',
      clipboard: '<path d="M9 5h-2a2 2 0 0 0 -2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2 -2v-12a2 2 0 0 0 -2 -2h-2"/><path d="M9 3m0 2a2 2 0 0 1 2 -2h2a2 2 0 0 1 2 2v0a2 2 0 0 1 -2 2h-2a2 2 0 0 1 -2 -2z"/>',
    };
    return `<svg data-tabler="${name}" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name]}</svg>`;
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
      const icon = NativeChatGPTPanel.tabler;
      const shortcut = key => `<button type="button" data-clip-action="ctrl-${key}" aria-label="Ctrl+${key.toUpperCase()} в Ubuntu" title="Ctrl+${key.toUpperCase()} в Ubuntu" style="display:inline-flex;align-items:center;gap:2px;height:24px;padding:0 6px">${icon('chevron-up')}${key.toUpperCase()}</button>`;
      this.innerHTML = '<div data-desktop-shell data-toolbar-collapsed="false" style="position:relative;height:100%;--ha-toolbar-height:28px">'
        + '<div style="height:var(--ha-toolbar-height);display:flex;align-items:center;gap:4px;padding:0 4px;box-sizing:border-box;background:var(--card-background-color);color:var(--primary-text-color);font:12px sans-serif">'
        + '<button type="button" data-toolbar-toggle aria-expanded="true" aria-label="Свернуть панель основного экрана Ubuntu" style="display:flex;align-items:center;justify-content:center;width:24px;height:24px;padding:0">'+icon('chevron-up')+'</button>'
        + '<button type="button" data-clipboard-toggle aria-label="Буфер обмена" title="Буфер обмена" style="display:flex;align-items:center;justify-content:center;width:24px;height:24px;padding:0">'+icon('clipboard')+'</button>'
        + shortcut('c') + shortcut('v') + '</div>'
        + '<section data-clipboard hidden style="position:absolute;z-index:10;top:var(--ha-toolbar-height);left:4px;right:4px;padding:8px;border:1px solid var(--divider-color);background:var(--card-background-color,#fff);color:var(--primary-text-color,#111);box-shadow:0 4px 12px #0004;font:13px sans-serif"><label>Текст между устройством и Ubuntu<textarea data-clip-text rows="4" spellcheck="false" style="display:block;box-sizing:border-box;width:100%;margin:6px 0;font:16px monospace" placeholder="Вставьте текст"></textarea></label><div style="display:flex;gap:6px;flex-wrap:wrap"><button type="button" data-clip-action="local">Вставить с устройства</button><button type="button" data-clip-action="send">На Ubuntu</button><button type="button" data-clip-action="receive">С Ubuntu</button><button type="button" data-clip-action="copy">Копировать на устройство</button></div><p data-clip-status role="status" aria-live="polite">Только текст, до 64 КБ.</p></section>'
        + '<iframe title="' + (this.desktopMode ? 'Ubuntu Desktop' : 'Native ChatGPT') + '" style="border:0;width:100%;height:calc(100% - var(--ha-toolbar-height));position:absolute;inset:var(--ha-toolbar-height) 0 0 0" allow="clipboard-read; clipboard-write"></iframe></div>';
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
