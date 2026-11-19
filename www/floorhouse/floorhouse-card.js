// ../../.bun/install/cache/links/@lit+reactive-element@2.1.2-65985871e396e82e/node_modules/@lit/reactive-element/css-tag.js
/**
* @license
* Copyright 2019 Google LLC
* SPDX-License-Identifier: BSD-3-Clause
*/
var t$1 = globalThis;
var e$2 = t$1.ShadowRoot && (void 0 === t$1.ShadyCSS || t$1.ShadyCSS.nativeShadow) && "adoptedStyleSheets" in Document.prototype && "replace" in CSSStyleSheet.prototype;
var s$2 = Symbol();
var o$4 = /* @__PURE__ */ new WeakMap();
var n$3 = class {
	constructor(t, e, o) {
		if (this._$cssResult$ = !0, o !== s$2) throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");
		this.cssText = t, this.t = e;
	}
	get styleSheet() {
		let t = this.o;
		const s = this.t;
		if (e$2 && void 0 === t) {
			const e = void 0 !== s && 1 === s.length;
			e && (t = o$4.get(s)), void 0 === t && ((this.o = t = new CSSStyleSheet()).replaceSync(this.cssText), e && o$4.set(s, t));
		}
		return t;
	}
	toString() {
		return this.cssText;
	}
};
var r$4 = (t) => new n$3("string" == typeof t ? t : t + "", void 0, s$2);
var i$3 = (t, ...e) => {
	return new n$3(1 === t.length ? t[0] : e.reduce((e, s, o) => e + ((t) => {
		if (!0 === t._$cssResult$) return t.cssText;
		if ("number" == typeof t) return t;
		throw Error("Value passed to 'css' function must be a 'css' function result: " + t + ". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.");
	})(s) + t[o + 1], t[0]), t, s$2);
};
var S$1 = (s, o) => {
	if (e$2) s.adoptedStyleSheets = o.map((t) => t instanceof CSSStyleSheet ? t : t.styleSheet);
	else for (const e of o) {
		const o = document.createElement("style"), n = t$1.litNonce;
		void 0 !== n && o.setAttribute("nonce", n), o.textContent = e.cssText, s.appendChild(o);
	}
};
var c$2 = e$2 ? (t) => t : (t) => t instanceof CSSStyleSheet ? ((t) => {
	let e = "";
	for (const s of t.cssRules) e += s.cssText;
	return r$4(e);
})(t) : t;
// ../../.bun/install/cache/links/@lit+reactive-element@2.1.2-65985871e396e82e/node_modules/@lit/reactive-element/reactive-element.js
/**
* @license
* Copyright 2017 Google LLC
* SPDX-License-Identifier: BSD-3-Clause
*/ var { is: i$2, defineProperty: e$1, getOwnPropertyDescriptor: h$1, getOwnPropertyNames: r$3, getOwnPropertySymbols: o$3, getPrototypeOf: n$2 } = Object, a$1 = globalThis, c$1 = a$1.trustedTypes, l$1 = c$1 ? c$1.emptyScript : "", p$1 = a$1.reactiveElementPolyfillSupport, d$1 = (t, s) => t, u$1 = {
	toAttribute(t, s) {
		switch (s) {
			case Boolean:
				t = t ? l$1 : null;
				break;
			case Object:
			case Array: t = null == t ? t : JSON.stringify(t);
		}
		return t;
	},
	fromAttribute(t, s) {
		let i = t;
		switch (s) {
			case Boolean:
				i = null !== t;
				break;
			case Number:
				i = null === t ? null : Number(t);
				break;
			case Object:
			case Array: try {
				i = JSON.parse(t);
			} catch (t) {
				i = null;
			}
		}
		return i;
	}
}, f$1 = (t, s) => !i$2(t, s), b$1 = {
	attribute: !0,
	type: String,
	converter: u$1,
	reflect: !1,
	useDefault: !1,
	hasChanged: f$1
};
Symbol.metadata ??= Symbol("metadata"), a$1.litPropertyMetadata ??= /* @__PURE__ */ new WeakMap();
var y$1 = class extends HTMLElement {
	static addInitializer(t) {
		this._$Ei(), (this.l ??= []).push(t);
	}
	static get observedAttributes() {
		return this.finalize(), this._$Eh && [...this._$Eh.keys()];
	}
	static createProperty(t, s = b$1) {
		if (s.state && (s.attribute = !1), this._$Ei(), this.prototype.hasOwnProperty(t) && ((s = Object.create(s)).wrapped = !0), this.elementProperties.set(t, s), !s.noAccessor) {
			const i = Symbol(), h = this.getPropertyDescriptor(t, i, s);
			void 0 !== h && e$1(this.prototype, t, h);
		}
	}
	static getPropertyDescriptor(t, s, i) {
		const { get: e, set: r } = h$1(this.prototype, t) ?? {
			get() {
				return this[s];
			},
			set(t) {
				this[s] = t;
			}
		};
		return {
			get: e,
			set(s) {
				const h = e?.call(this);
				r?.call(this, s), this.requestUpdate(t, h, i);
			},
			configurable: !0,
			enumerable: !0
		};
	}
	static getPropertyOptions(t) {
		return this.elementProperties.get(t) ?? b$1;
	}
	static _$Ei() {
		if (this.hasOwnProperty(d$1("elementProperties"))) return;
		const t = n$2(this);
		t.finalize(), void 0 !== t.l && (this.l = [...t.l]), this.elementProperties = new Map(t.elementProperties);
	}
	static finalize() {
		if (this.hasOwnProperty(d$1("finalized"))) return;
		if (this.finalized = !0, this._$Ei(), this.hasOwnProperty(d$1("properties"))) {
			const t = this.properties, s = [...r$3(t), ...o$3(t)];
			for (const i of s) this.createProperty(i, t[i]);
		}
		const t = this[Symbol.metadata];
		if (null !== t) {
			const s = litPropertyMetadata.get(t);
			if (void 0 !== s) for (const [t, i] of s) this.elementProperties.set(t, i);
		}
		this._$Eh = /* @__PURE__ */ new Map();
		for (const [t, s] of this.elementProperties) {
			const i = this._$Eu(t, s);
			void 0 !== i && this._$Eh.set(i, t);
		}
		this.elementStyles = this.finalizeStyles(this.styles);
	}
	static finalizeStyles(s) {
		const i = [];
		if (Array.isArray(s)) {
			const e = new Set(s.flat(1 / 0).reverse());
			for (const s of e) i.unshift(c$2(s));
		} else void 0 !== s && i.push(c$2(s));
		return i;
	}
	static _$Eu(t, s) {
		const i = s.attribute;
		return !1 === i ? void 0 : "string" == typeof i ? i : "string" == typeof t ? t.toLowerCase() : void 0;
	}
	constructor() {
		super(), this._$Ep = void 0, this.isUpdatePending = !1, this.hasUpdated = !1, this._$Em = null, this._$Ev();
	}
	_$Ev() {
		this._$ES = new Promise((t) => this.enableUpdating = t), this._$AL = /* @__PURE__ */ new Map(), this._$E_(), this.requestUpdate(), this.constructor.l?.forEach((t) => t(this));
	}
	addController(t) {
		(this._$EO ??= /* @__PURE__ */ new Set()).add(t), void 0 !== this.renderRoot && this.isConnected && t.hostConnected?.();
	}
	removeController(t) {
		this._$EO?.delete(t);
	}
	_$E_() {
		const t = /* @__PURE__ */ new Map(), s = this.constructor.elementProperties;
		for (const i of s.keys()) this.hasOwnProperty(i) && (t.set(i, this[i]), delete this[i]);
		t.size > 0 && (this._$Ep = t);
	}
	createRenderRoot() {
		const t = this.shadowRoot ?? this.attachShadow(this.constructor.shadowRootOptions);
		return S$1(t, this.constructor.elementStyles), t;
	}
	connectedCallback() {
		this.renderRoot ??= this.createRenderRoot(), this.enableUpdating(!0), this._$EO?.forEach((t) => t.hostConnected?.());
	}
	enableUpdating(t) {}
	disconnectedCallback() {
		this._$EO?.forEach((t) => t.hostDisconnected?.());
	}
	attributeChangedCallback(t, s, i) {
		this._$AK(t, i);
	}
	_$ET(t, s) {
		const i = this.constructor.elementProperties.get(t), e = this.constructor._$Eu(t, i);
		if (void 0 !== e && !0 === i.reflect) {
			const h = (void 0 !== i.converter?.toAttribute ? i.converter : u$1).toAttribute(s, i.type);
			this._$Em = t, null == h ? this.removeAttribute(e) : this.setAttribute(e, h), this._$Em = null;
		}
	}
	_$AK(t, s) {
		const i = this.constructor, e = i._$Eh.get(t);
		if (void 0 !== e && this._$Em !== e) {
			const t = i.getPropertyOptions(e), h = "function" == typeof t.converter ? { fromAttribute: t.converter } : void 0 !== t.converter?.fromAttribute ? t.converter : u$1;
			this._$Em = e;
			const r = h.fromAttribute(s, t.type);
			this[e] = r ?? this._$Ej?.get(e) ?? r, this._$Em = null;
		}
	}
	requestUpdate(t, s, i, e = !1, h) {
		if (void 0 !== t) {
			const r = this.constructor;
			if (!1 === e && (h = this[t]), i ??= r.getPropertyOptions(t), !((i.hasChanged ?? f$1)(h, s) || i.useDefault && i.reflect && h === this._$Ej?.get(t) && !this.hasAttribute(r._$Eu(t, i)))) return;
			this.C(t, s, i);
		}
		!1 === this.isUpdatePending && (this._$ES = this._$EP());
	}
	C(t, s, { useDefault: i, reflect: e, wrapped: h }, r) {
		i && !(this._$Ej ??= /* @__PURE__ */ new Map()).has(t) && (this._$Ej.set(t, r ?? s ?? this[t]), !0 !== h || void 0 !== r) || (this._$AL.has(t) || (this.hasUpdated || i || (s = void 0), this._$AL.set(t, s)), !0 === e && this._$Em !== t && (this._$Eq ??= /* @__PURE__ */ new Set()).add(t));
	}
	async _$EP() {
		this.isUpdatePending = !0;
		try {
			await this._$ES;
		} catch (t) {
			Promise.reject(t);
		}
		const t = this.scheduleUpdate();
		return null != t && await t, !this.isUpdatePending;
	}
	scheduleUpdate() {
		return this.performUpdate();
	}
	performUpdate() {
		if (!this.isUpdatePending) return;
		if (!this.hasUpdated) {
			if (this.renderRoot ??= this.createRenderRoot(), this._$Ep) {
				for (const [t, s] of this._$Ep) this[t] = s;
				this._$Ep = void 0;
			}
			const t = this.constructor.elementProperties;
			if (t.size > 0) for (const [s, i] of t) {
				const { wrapped: t } = i, e = this[s];
				!0 !== t || this._$AL.has(s) || void 0 === e || this.C(s, void 0, i, e);
			}
		}
		let t = !1;
		const s = this._$AL;
		try {
			t = this.shouldUpdate(s), t ? (this.willUpdate(s), this._$EO?.forEach((t) => t.hostUpdate?.()), this.update(s)) : this._$EM();
		} catch (s) {
			throw t = !1, this._$EM(), s;
		}
		t && this._$AE(s);
	}
	willUpdate(t) {}
	_$AE(t) {
		this._$EO?.forEach((t) => t.hostUpdated?.()), this.hasUpdated || (this.hasUpdated = !0, this.firstUpdated(t)), this.updated(t);
	}
	_$EM() {
		this._$AL = /* @__PURE__ */ new Map(), this.isUpdatePending = !1;
	}
	get updateComplete() {
		return this.getUpdateComplete();
	}
	getUpdateComplete() {
		return this._$ES;
	}
	shouldUpdate(t) {
		return !0;
	}
	update(t) {
		this._$Eq &&= this._$Eq.forEach((t) => this._$ET(t, this[t])), this._$EM();
	}
	updated(t) {}
	firstUpdated(t) {}
};
y$1.elementStyles = [], y$1.shadowRootOptions = { mode: "open" }, y$1[d$1("elementProperties")] = /* @__PURE__ */ new Map(), y$1[d$1("finalized")] = /* @__PURE__ */ new Map(), p$1?.({ ReactiveElement: y$1 }), (a$1.reactiveElementVersions ??= []).push("2.1.2");
// ../../.bun/install/cache/links/lit-html@3.3.3-82e672efdb4c2949/node_modules/lit-html/lit-html.js
/**
* @license
* Copyright 2017 Google LLC
* SPDX-License-Identifier: BSD-3-Clause
*/
var t = globalThis;
var i$1 = (t) => t;
var s$1 = t.trustedTypes;
var e = s$1 ? s$1.createPolicy("lit-html", { createHTML: (t) => t }) : void 0;
var h = "$lit$";
var o$2 = `lit$${Math.random().toFixed(9).slice(2)}$`;
var n$1 = "?" + o$2;
var r$2 = `<${n$1}>`;
var l = document;
var c = () => l.createComment("");
var a = (t) => null === t || "object" != typeof t && "function" != typeof t;
var u = Array.isArray;
var d = (t) => u(t) || "function" == typeof t?.[Symbol.iterator];
var f = "[ 	\n\f\r]";
var v = /<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g;
var _ = /-->/g;
var m = />/g;
var p = RegExp(`>|${f}(?:([^\\s"'>=/]+)(${f}*=${f}*(?:[^ \t\n\f\r"'\`<>=]|("|')|))|$)`, "g");
var g = /'/g;
var $ = /"/g;
var y = /^(?:script|style|textarea|title)$/i;
var x = (t) => (i, ...s) => ({
	_$litType$: t,
	strings: i,
	values: s
});
var b = x(1);
var w = x(2);
var E = Symbol.for("lit-noChange");
var A = Symbol.for("lit-nothing");
var C = /* @__PURE__ */ new WeakMap();
var P = l.createTreeWalker(l, 129);
function V(t, i) {
	if (!u(t) || !t.hasOwnProperty("raw")) throw Error("invalid template strings array");
	return void 0 !== e ? e.createHTML(i) : i;
}
var N = (t, i) => {
	const s = t.length - 1, e = [];
	let n, l = 2 === i ? "<svg>" : 3 === i ? "<math>" : "", c = v;
	for (let i = 0; i < s; i++) {
		const s = t[i];
		let a, u, d = -1, f = 0;
		for (; f < s.length && (c.lastIndex = f, u = c.exec(s), null !== u);) f = c.lastIndex, c === v ? "!--" === u[1] ? c = _ : void 0 !== u[1] ? c = m : void 0 !== u[2] ? (y.test(u[2]) && (n = RegExp("</" + u[2], "g")), c = p) : void 0 !== u[3] && (c = p) : c === p ? ">" === u[0] ? (c = n ?? v, d = -1) : void 0 === u[1] ? d = -2 : (d = c.lastIndex - u[2].length, a = u[1], c = void 0 === u[3] ? p : "\"" === u[3] ? $ : g) : c === $ || c === g ? c = p : c === _ || c === m ? c = v : (c = p, n = void 0);
		const x = c === p && t[i + 1].startsWith("/>") ? " " : "";
		l += c === v ? s + r$2 : d >= 0 ? (e.push(a), s.slice(0, d) + h + s.slice(d) + o$2 + x) : s + o$2 + (-2 === d ? i : x);
	}
	return [V(t, l + (t[s] || "<?>") + (2 === i ? "</svg>" : 3 === i ? "</math>" : "")), e];
};
var S = class S {
	constructor({ strings: t, _$litType$: i }, e) {
		let r;
		this.parts = [];
		let l = 0, a = 0;
		const u = t.length - 1, d = this.parts, [f, v] = N(t, i);
		if (this.el = S.createElement(f, e), P.currentNode = this.el.content, 2 === i || 3 === i) {
			const t = this.el.content.firstChild;
			t.replaceWith(...t.childNodes);
		}
		for (; null !== (r = P.nextNode()) && d.length < u;) {
			if (1 === r.nodeType) {
				if (r.hasAttributes()) for (const t of r.getAttributeNames()) if (t.endsWith(h)) {
					const i = v[a++], s = r.getAttribute(t).split(o$2), e = /([.?@])?(.*)/.exec(i);
					d.push({
						type: 1,
						index: l,
						name: e[2],
						strings: s,
						ctor: "." === e[1] ? I : "?" === e[1] ? L : "@" === e[1] ? z : H
					}), r.removeAttribute(t);
				} else t.startsWith(o$2) && (d.push({
					type: 6,
					index: l
				}), r.removeAttribute(t));
				if (y.test(r.tagName)) {
					const t = r.textContent.split(o$2), i = t.length - 1;
					if (i > 0) {
						r.textContent = s$1 ? s$1.emptyScript : "";
						for (let s = 0; s < i; s++) r.append(t[s], c()), P.nextNode(), d.push({
							type: 2,
							index: ++l
						});
						r.append(t[i], c());
					}
				}
			} else if (8 === r.nodeType) if (r.data === n$1) d.push({
				type: 2,
				index: l
			});
			else {
				let t = -1;
				for (; -1 !== (t = r.data.indexOf(o$2, t + 1));) d.push({
					type: 7,
					index: l
				}), t += o$2.length - 1;
			}
			l++;
		}
	}
	static createElement(t, i) {
		const s = l.createElement("template");
		return s.innerHTML = t, s;
	}
};
function M(t, i, s = t, e) {
	if (i === E) return i;
	let h = void 0 !== e ? s._$Co?.[e] : s._$Cl;
	const o = a(i) ? void 0 : i._$litDirective$;
	return h?.constructor !== o && (h?._$AO?.(!1), void 0 === o ? h = void 0 : (h = new o(t), h._$AT(t, s, e)), void 0 !== e ? (s._$Co ??= [])[e] = h : s._$Cl = h), void 0 !== h && (i = M(t, h._$AS(t, i.values), h, e)), i;
}
var R = class {
	constructor(t, i) {
		this._$AV = [], this._$AN = void 0, this._$AD = t, this._$AM = i;
	}
	get parentNode() {
		return this._$AM.parentNode;
	}
	get _$AU() {
		return this._$AM._$AU;
	}
	u(t) {
		const { el: { content: i }, parts: s } = this._$AD, e = (t?.creationScope ?? l).importNode(i, !0);
		P.currentNode = e;
		let h = P.nextNode(), o = 0, n = 0, r = s[0];
		for (; void 0 !== r;) {
			if (o === r.index) {
				let i;
				2 === r.type ? i = new k(h, h.nextSibling, this, t) : 1 === r.type ? i = new r.ctor(h, r.name, r.strings, this, t) : 6 === r.type && (i = new Z(h, this, t)), this._$AV.push(i), r = s[++n];
			}
			o !== r?.index && (h = P.nextNode(), o++);
		}
		return P.currentNode = l, e;
	}
	p(t) {
		let i = 0;
		for (const s of this._$AV) void 0 !== s && (void 0 !== s.strings ? (s._$AI(t, s, i), i += s.strings.length - 2) : s._$AI(t[i])), i++;
	}
};
var k = class k {
	get _$AU() {
		return this._$AM?._$AU ?? this._$Cv;
	}
	constructor(t, i, s, e) {
		this.type = 2, this._$AH = A, this._$AN = void 0, this._$AA = t, this._$AB = i, this._$AM = s, this.options = e, this._$Cv = e?.isConnected ?? !0;
	}
	get parentNode() {
		let t = this._$AA.parentNode;
		const i = this._$AM;
		return void 0 !== i && 11 === t?.nodeType && (t = i.parentNode), t;
	}
	get startNode() {
		return this._$AA;
	}
	get endNode() {
		return this._$AB;
	}
	_$AI(t, i = this) {
		t = M(this, t, i), a(t) ? t === A || null == t || "" === t ? (this._$AH !== A && this._$AR(), this._$AH = A) : t !== this._$AH && t !== E && this._(t) : void 0 !== t._$litType$ ? this.$(t) : void 0 !== t.nodeType ? this.T(t) : d(t) ? this.k(t) : this._(t);
	}
	O(t) {
		return this._$AA.parentNode.insertBefore(t, this._$AB);
	}
	T(t) {
		this._$AH !== t && (this._$AR(), this._$AH = this.O(t));
	}
	_(t) {
		this._$AH !== A && a(this._$AH) ? this._$AA.nextSibling.data = t : this.T(l.createTextNode(t)), this._$AH = t;
	}
	$(t) {
		const { values: i, _$litType$: s } = t, e = "number" == typeof s ? this._$AC(t) : (void 0 === s.el && (s.el = S.createElement(V(s.h, s.h[0]), this.options)), s);
		if (this._$AH?._$AD === e) this._$AH.p(i);
		else {
			const t = new R(e, this), s = t.u(this.options);
			t.p(i), this.T(s), this._$AH = t;
		}
	}
	_$AC(t) {
		let i = C.get(t.strings);
		return void 0 === i && C.set(t.strings, i = new S(t)), i;
	}
	k(t) {
		u(this._$AH) || (this._$AH = [], this._$AR());
		const i = this._$AH;
		let s, e = 0;
		for (const h of t) e === i.length ? i.push(s = new k(this.O(c()), this.O(c()), this, this.options)) : s = i[e], s._$AI(h), e++;
		e < i.length && (this._$AR(s && s._$AB.nextSibling, e), i.length = e);
	}
	_$AR(t = this._$AA.nextSibling, s) {
		for (this._$AP?.(!1, !0, s); t !== this._$AB;) {
			const s = i$1(t).nextSibling;
			i$1(t).remove(), t = s;
		}
	}
	setConnected(t) {
		void 0 === this._$AM && (this._$Cv = t, this._$AP?.(t));
	}
};
var H = class {
	get tagName() {
		return this.element.tagName;
	}
	get _$AU() {
		return this._$AM._$AU;
	}
	constructor(t, i, s, e, h) {
		this.type = 1, this._$AH = A, this._$AN = void 0, this.element = t, this.name = i, this._$AM = e, this.options = h, s.length > 2 || "" !== s[0] || "" !== s[1] ? (this._$AH = Array(s.length - 1).fill(/* @__PURE__ */ new String()), this.strings = s) : this._$AH = A;
	}
	_$AI(t, i = this, s, e) {
		const h = this.strings;
		let o = !1;
		if (void 0 === h) t = M(this, t, i, 0), o = !a(t) || t !== this._$AH && t !== E, o && (this._$AH = t);
		else {
			const e = t;
			let n, r;
			for (t = h[0], n = 0; n < h.length - 1; n++) r = M(this, e[s + n], i, n), r === E && (r = this._$AH[n]), o ||= !a(r) || r !== this._$AH[n], r === A ? t = A : t !== A && (t += (r ?? "") + h[n + 1]), this._$AH[n] = r;
		}
		o && !e && this.j(t);
	}
	j(t) {
		t === A ? this.element.removeAttribute(this.name) : this.element.setAttribute(this.name, t ?? "");
	}
};
var I = class extends H {
	constructor() {
		super(...arguments), this.type = 3;
	}
	j(t) {
		this.element[this.name] = t === A ? void 0 : t;
	}
};
var L = class extends H {
	constructor() {
		super(...arguments), this.type = 4;
	}
	j(t) {
		this.element.toggleAttribute(this.name, !!t && t !== A);
	}
};
var z = class extends H {
	constructor(t, i, s, e, h) {
		super(t, i, s, e, h), this.type = 5;
	}
	_$AI(t, i = this) {
		if ((t = M(this, t, i, 0) ?? A) === E) return;
		const s = this._$AH, e = t === A && s !== A || t.capture !== s.capture || t.once !== s.once || t.passive !== s.passive, h = t !== A && (s === A || e);
		e && this.element.removeEventListener(this.name, this, s), h && this.element.addEventListener(this.name, this, t), this._$AH = t;
	}
	handleEvent(t) {
		"function" == typeof this._$AH ? this._$AH.call(this.options?.host ?? this.element, t) : this._$AH.handleEvent(t);
	}
};
var Z = class {
	constructor(t, i, s) {
		this.element = t, this.type = 6, this._$AN = void 0, this._$AM = i, this.options = s;
	}
	get _$AU() {
		return this._$AM._$AU;
	}
	_$AI(t) {
		M(this, t);
	}
};
var B = t.litHtmlPolyfillSupport;
B?.(S, k), (t.litHtmlVersions ??= []).push("3.3.3");
var D = (t, i, s) => {
	const e = s?.renderBefore ?? i;
	let h = e._$litPart$;
	if (void 0 === h) {
		const t = s?.renderBefore ?? null;
		e._$litPart$ = h = new k(i.insertBefore(c(), t), t, void 0, s ?? {});
	}
	return h._$AI(t), h;
};
// ../../.bun/install/cache/links/lit-element@4.2.2-ed861355cdd17f34/node_modules/lit-element/lit-element.js
/**
* @license
* Copyright 2017 Google LLC
* SPDX-License-Identifier: BSD-3-Clause
*/ var s = globalThis;
var i = class extends y$1 {
	constructor() {
		super(...arguments), this.renderOptions = { host: this }, this._$Do = void 0;
	}
	createRenderRoot() {
		const t = super.createRenderRoot();
		return this.renderOptions.renderBefore ??= t.firstChild, t;
	}
	update(t) {
		const r = this.render();
		this.hasUpdated || (this.renderOptions.isConnected = this.isConnected), super.update(t), this._$Do = D(r, this.renderRoot, this.renderOptions);
	}
	connectedCallback() {
		super.connectedCallback(), this._$Do?.setConnected(!0);
	}
	disconnectedCallback() {
		super.disconnectedCallback(), this._$Do?.setConnected(!1);
	}
	render() {
		return E;
	}
};
i._$litElement$ = !0, i["finalized"] = !0, s.litElementHydrateSupport?.({ LitElement: i });
var o$1 = s.litElementPolyfillSupport;
o$1?.({ LitElement: i });
(s.litElementVersions ??= []).push("4.2.2");
// ../../.bun/install/cache/links/@lit+reactive-element@2.1.2-65985871e396e82e/node_modules/@lit/reactive-element/decorators/property.js
/**
* @license
* Copyright 2017 Google LLC
* SPDX-License-Identifier: BSD-3-Clause
*/ var o = {
	attribute: !0,
	type: String,
	converter: u$1,
	reflect: !1,
	hasChanged: f$1
};
var r$1 = (t = o, e, r) => {
	const { kind: n, metadata: i } = r;
	let s = globalThis.litPropertyMetadata.get(i);
	if (void 0 === s && globalThis.litPropertyMetadata.set(i, s = /* @__PURE__ */ new Map()), "setter" === n && ((t = Object.create(t)).wrapped = !0), s.set(r.name, t), "accessor" === n) {
		const { name: o } = r;
		return {
			set(r) {
				const n = e.get.call(this);
				e.set.call(this, r), this.requestUpdate(o, n, t, !0, r);
			},
			init(e) {
				return void 0 !== e && this.C(o, void 0, t, e), e;
			}
		};
	}
	if ("setter" === n) {
		const { name: o } = r;
		return function(r) {
			const n = this[o];
			e.call(this, r), this.requestUpdate(o, n, t, !0, r);
		};
	}
	throw Error("Unsupported decorator location: " + n);
};
function n(t) {
	return (e, o) => "object" == typeof o ? r$1(t, e, o) : ((t, e, o) => {
		const r = e.hasOwnProperty(o);
		return e.constructor.createProperty(o, t), r ? Object.getOwnPropertyDescriptor(e, o) : void 0;
	})(t, e, o);
}
// ../../.bun/install/cache/links/@lit+reactive-element@2.1.2-65985871e396e82e/node_modules/@lit/reactive-element/decorators/state.js
/**
* @license
* Copyright 2017 Google LLC
* SPDX-License-Identifier: BSD-3-Clause
*/ function r(r) {
	return n({
		...r,
		state: !0,
		attribute: !1
	});
}
// src/types.ts
function isLightObject(object) {
	return object.type === "light";
}
// src/config.ts
var DEFAULT_METERS_PER_CELL = .5;
/** Chip kinds, shared with the renderer (review cleanup: was duplicated). */
var CHIP_KINDS = [
	"temperature",
	"humidity",
	"co2"
];
/**
* Climate-chip exclusion REGEXES — a USER override only (review finding: the
* default 'почв' pattern could never match, since HA entity ids are ASCII).
* The built-in protection is the device_class whitelist in render/floorplan.
*/
var DEFAULT_CLIMATE_EXCLUDE = [];
var FURNITURE_TYPES = [
	"couch",
	"couch-l",
	"bed",
	"table",
	"chair",
	"plant",
	"tv",
	"desk",
	"counter",
	"stairs"
];
var LIGHT_FIXTURES = [
	"point",
	"strip",
	"ceiling"
];
var OBJECT_TYPES = [...FURNITURE_TYPES, "light"];
var WALL_SIDES = [
	"left",
	"right",
	"top",
	"bottom"
];
var SLOT_KINDS = [
	"light",
	"media_player",
	"sensor",
	"plant",
	"printer",
	"network",
	"vacuum",
	"switch",
	"climate",
	"embed"
];
/** Tile size in cells per kind (spec §3.5: compact 1–2 cell tiles). */
var DEFAULT_SLOT_SIZE = {
	light: {
		w: 1,
		h: 1
	},
	media_player: {
		w: 2,
		h: 1
	},
	sensor: {
		w: 1,
		h: 1
	},
	plant: {
		w: 1,
		h: 1
	},
	printer: {
		w: 2,
		h: 1
	},
	network: {
		w: 1,
		h: 1
	},
	vacuum: {
		w: 2,
		h: 1
	},
	switch: {
		w: 1,
		h: 1
	},
	climate: {
		w: 1,
		h: 1
	},
	embed: {
		w: 1,
		h: 1
	}
};
var EPS$1 = 1e-9;
/**
* The default home, in cells (grid 8×10 per floor, meters_per_cell 0.5).
*
* Floor 1 mirrors the owner sketch: hall top-left with the entrance door in
* the left wall, corridor strip down the left, WC in the middle opening to
* the corridor, kitchen upper-right with a counter strip along its right
* edge and the door at its lower-left, a desk nook strip above the kitchen,
* the «2fl» stairwell continuing the left strip, and the living room across
* the bottom. Floor 2 is the plausible default the owner corrects visually
* (Спальня, Студия, Угол, «Второй этаж» landing); the stairwell cell range
* matches floor 1 because physically it is the same opening.
*/
var DEFAULT_FLOORS = [{
	id: "floor1",
	name: "1 этаж",
	rooms: [
		{
			id: "prikhozhaiа",
			name: "Прихожая",
			area_hint: "prikhozhaiа",
			x: 0,
			y: 0,
			w: 3,
			h: 3,
			doors: [{
				wall: "left",
				at: 1.1,
				size: .8
			}],
			sensors: {
				temperature: "sensor.prikhozhaya_temperature",
				humidity: "sensor.prikhozhaya_humidity"
			},
			slots: [{
				kind: "switch",
				entity: "switch.prikhozhaya_socket",
				x: 2,
				y: .2
			}]
		},
		{
			id: "koridor",
			name: "Коридор",
			x: 0,
			y: 3,
			w: 2,
			h: 4
		},
		{
			id: "wc",
			name: "Санузел",
			x: 2,
			y: 3,
			w: 2,
			h: 2,
			doors: [{
				wall: "left",
				at: .6,
				size: .7
			}]
		},
		{
			id: "stairs",
			name: "2fl",
			x: 0,
			y: 7,
			w: 2,
			h: 3
		},
		{
			id: "desk",
			name: "Рабочее место",
			x: 4,
			y: 0,
			w: 4,
			h: 1
		},
		{
			id: "kukhnia",
			name: "Кухня",
			area_hint: "kukhnia",
			x: 4,
			y: 1,
			w: 4,
			h: 4,
			doors: [{
				wall: "bottom",
				at: .6,
				size: .9
			}],
			sensors: {
				temperature: "sensor.kukhnia_temperature",
				humidity: "sensor.kukhnia_humidity"
			},
			slots: [{
				kind: "light",
				entity: "light.kukhnia_podsvetka",
				x: 2.2,
				y: .2
			}]
		},
		{
			id: "gostinaia",
			name: "Гостиная",
			area_hint: "gostinaia",
			x: 2,
			y: 5,
			w: 6,
			h: 5,
			doors: [{
				wall: "left",
				at: .8,
				size: 1
			}],
			sensors: {
				temperature: "sensor.gostinaia_temperature",
				humidity: "sensor.gostinaia_humidity",
				co2: "sensor.gostinaia_co2"
			},
			slots: [{
				kind: "media_player",
				entity: "media_player.gostinaia_tv",
				x: 4,
				y: 3.9
			}, {
				kind: "sensor",
				entity: "sensor.gostinaia_co2",
				secondary: "sensor.gostinaia_temperature",
				title: "CO₂ · t°",
				x: .3,
				y: .55
			}]
		}
	],
	objects: [
		{
			type: "plant",
			x: 2.2,
			y: 2.25,
			w: .6,
			h: .6
		},
		{
			type: "plant",
			x: .35,
			y: 6.25,
			w: .65,
			h: .65
		},
		{
			type: "desk",
			x: 2.15,
			y: 3.15,
			w: 1.7,
			h: .5
		},
		{
			type: "counter",
			x: 7.05,
			y: 1.15,
			w: .8,
			h: 3.7
		},
		{
			type: "table",
			x: 4.6,
			y: 2.5,
			w: 1.7,
			h: 1.2
		},
		{
			type: "chair",
			x: 4.9,
			y: 1.6,
			w: .6,
			h: .6
		},
		{
			type: "chair",
			x: 4.9,
			y: 3.85,
			w: .6,
			h: .6
		},
		{
			type: "desk",
			x: 4.2,
			y: .3,
			w: 3.6,
			h: .45
		},
		{
			type: "stairs",
			x: .45,
			y: 7.25,
			w: 1.1,
			h: 2.5
		},
		{
			type: "couch-l",
			x: 2.15,
			y: 7.9,
			w: 1.7,
			h: 1.85
		},
		{
			type: "table",
			x: 3.6,
			y: 6.9,
			w: 1.3,
			h: .9
		},
		{
			type: "chair",
			x: 6.9,
			y: 5.25,
			w: .6,
			h: .6
		},
		{
			type: "tv",
			x: 7.8,
			y: 6.6,
			w: .18,
			h: 1.8
		},
		{
			type: "plant",
			x: 7.25,
			y: 9.2,
			w: .6,
			h: .6
		},
		{
			type: "light",
			entity_id: "light.gostinaia_lustra",
			fixture: "ceiling",
			x: 5,
			y: 7.5
		},
		{
			type: "light",
			entity_id: "light.gostinaia_bra",
			fixture: "point",
			x: 7.2,
			y: 5.4
		},
		{
			type: "light",
			entity_id: "light.prikhozhaya",
			fixture: "point",
			x: 1.5,
			y: 1.5
		},
		{
			type: "light",
			entity_id: "light.koridor_led",
			fixture: "strip",
			x: 1,
			y: 5,
			polyline: [
				[.18, 3.25],
				[.18, 6.75],
				[1.82, 6.75],
				[1.82, 3.25]
			]
		},
		{
			type: "light",
			entity_id: "light.kukhnia_podsvetka",
			fixture: "point",
			x: 6.2,
			y: 3
		}
	]
}, {
	id: "floor2",
	name: "2 этаж",
	rooms: [
		{
			id: "spalnia",
			name: "Спальня",
			area_hint: "spalnia",
			x: 0,
			y: 0,
			w: 4,
			h: 5,
			doors: [{
				wall: "bottom",
				at: .8,
				size: .9
			}],
			sensors: {
				temperature: "sensor.spalnia_temperature",
				humidity: "sensor.spalnia_humidity",
				co2: "sensor.spalnia_co2"
			},
			slots: [{
				kind: "sensor",
				entity: "sensor.spalnia_temperature",
				secondary: "sensor.spalnia_humidity",
				title: "t° · RH",
				x: 1,
				y: 3.9
			}]
		},
		{
			id: "studia",
			name: "Студия",
			area_hint: "studia",
			x: 4,
			y: 0,
			w: 4,
			h: 5,
			doors: [{
				wall: "bottom",
				at: .8,
				size: .9
			}],
			sensors: { temperature: "sensor.studia_temperature" },
			slots: [{
				kind: "media_player",
				entity: "media_player.aisis_speaker_m00zrw3008rajg",
				extras: {
					connection: "sensor.aisis_speaker_m00zrw3008rajg_connected",
					sound_level: "sensor.aisis_speaker_m00zrw3008rajg_sound_level"
				},
				x: 0,
				y: 2
			}, {
				kind: "printer",
				entity: "sensor.printer_3d",
				x: 2,
				y: 2
			}]
		},
		{
			id: "ugol",
			name: "Угол",
			area_hint: "ugol",
			x: 0,
			y: 5,
			w: 2,
			h: 2,
			doors: [{
				wall: "right",
				at: .5,
				size: .7
			}],
			slots: [{
				kind: "plant",
				entity: "plant.monstera",
				extras: { moisture: "sensor.ugol_soil_moisture" },
				x: .95,
				y: .15
			}]
		},
		{
			id: "vtoroi_etazh",
			name: "Второй этаж",
			area_hint: "vtoroi_etazh",
			x: 2,
			y: 5,
			w: 6,
			h: 5,
			sensors: {
				temperature: "sensor.vtoroi_etazh_temperature",
				humidity: "sensor.vtoroi_etazh_humidity",
				co2: "sensor.vtoroi_etazh_co2"
			}
		},
		{
			id: "stairs",
			name: "1fl",
			x: 0,
			y: 7,
			w: 2,
			h: 3
		}
	],
	objects: [
		{
			type: "bed",
			x: .35,
			y: .4,
			w: 2.3,
			h: 3.1
		},
		{
			type: "chair",
			x: 3.1,
			y: .45,
			w: .65,
			h: .65
		},
		{
			type: "plant",
			x: 3.05,
			y: 4.2,
			w: .6,
			h: .6
		},
		{
			type: "desk",
			x: 4.4,
			y: .35,
			w: 3.2,
			h: .6
		},
		{
			type: "chair",
			x: 5.5,
			y: 1.15,
			w: .65,
			h: .65
		},
		{
			type: "couch",
			x: 4.4,
			y: 3.9,
			w: 2.4,
			h: .75,
			rot: 180
		},
		{
			type: "plant",
			x: 7.2,
			y: .35,
			w: .65,
			h: .65
		},
		{
			type: "plant",
			x: .15,
			y: 5.95,
			w: .7,
			h: .7
		},
		{
			type: "chair",
			x: 1.15,
			y: 6.15,
			w: .65,
			h: .65
		},
		{
			type: "table",
			x: 4.4,
			y: 6.6,
			w: 1.6,
			h: 1.1
		},
		{
			type: "chair",
			x: 4.65,
			y: 5.55,
			w: .6,
			h: .6
		},
		{
			type: "chair",
			x: 4.65,
			y: 8.15,
			w: .6,
			h: .6
		},
		{
			type: "couch",
			x: 6.3,
			y: 8.9,
			w: 1.5,
			h: .8
		},
		{
			type: "tv",
			x: 7.8,
			y: 6.4,
			w: .18,
			h: 1.5
		},
		{
			type: "stairs",
			x: .45,
			y: 7.25,
			w: 1.1,
			h: 2.5
		},
		{
			type: "light",
			entity_id: "light.spalnia_liustra",
			fixture: "ceiling",
			x: 2,
			y: 2.5
		},
		{
			type: "light",
			entity_id: "light.spalnia_nochnik",
			fixture: "point",
			x: 2.85,
			y: 1.05
		},
		{
			type: "light",
			entity_id: "light.studia_potolok",
			fixture: "ceiling",
			x: 6,
			y: 2.5
		}
	]
}];
/**
* Default dock: entities without a room slot (spec §3.1) — a network device,
* one unassigned light and an embedded-card demo tile.
*/
var DEFAULT_DOCK = [
	{
		kind: "network",
		entity: "device_tracker.ultra_phone",
		link: "/system-metrics"
	},
	{
		kind: "light",
		entity: "light.spalnia_liustra"
	},
	{
		kind: "embed",
		title: "Markdown",
		card: {
			type: "markdown",
			content: "## Встроенная карточка\nСтандартный Lovelace рендерится в поповере в натуральном размере."
		}
	}
];
/** Grid cell size in px, clamped into CELL_PX_MIN..CELL_PX_MAX. */
function clampCellPx(px) {
	return Math.min(80, Math.max(40, Math.round(px)));
}
/** Compiled patterns cache — the parsed config's array reference is stable,
* so regexes compile once instead of on every hass render (review cleanup). */
var compiledExcludes = /* @__PURE__ */ new WeakMap();
/** True when the entity id matches any climate-exclude regex (case-insensitive). */
function isClimateExcluded(entityId, patterns) {
	let compiled = compiledExcludes.get(patterns);
	if (compiled === void 0) {
		compiled = patterns.map((pattern) => new RegExp(pattern, "i"));
		compiledExcludes.set(patterns, compiled);
	}
	return compiled.some((regex) => regex.test(entityId));
}
/** Error message when two rooms' interiors intersect (shared edges are fine). */
function roomsOverlapError(rooms) {
	for (const [i, a] of rooms.entries()) for (const b of rooms.slice(i + 1)) if (a.x < b.x + b.w - EPS$1 && b.x < a.x + a.w - EPS$1 && a.y < b.y + b.h - EPS$1 && b.y < a.y + a.h - EPS$1) return `floorhouse-card: rooms "${a.id}" and "${b.id}" overlap`;
	return null;
}
/** Error message when an object lies outside the floor's room bounding box. */
function furniturePlacementError(floor) {
	let minX = Infinity;
	let minY = Infinity;
	let maxX = -Infinity;
	let maxY = -Infinity;
	for (const room of floor.rooms) {
		minX = Math.min(minX, room.x);
		minY = Math.min(minY, room.y);
		maxX = Math.max(maxX, room.x + room.w);
		maxY = Math.max(maxY, room.y + room.h);
	}
	for (const obj of floor.objects ?? []) {
		const points = isLightObject(obj) ? [...obj.polyline ?? [], [obj.x, obj.y]] : [[obj.x, obj.y], [obj.x + obj.w, obj.y + obj.h]];
		for (const [px, py] of points) if (px < minX - EPS$1 || py < minY - EPS$1 || px > maxX + EPS$1 || py > maxY + EPS$1) {
			const what = isLightObject(obj) ? `light ${obj.entity_id}` : `object ${obj.type}`;
			return `floorhouse-card: floor "${floor.id}": ${what} at (${obj.x}, ${obj.y}) lies outside the floor grid ${minX}..${maxX} × ${minY}..${maxY}`;
		}
	}
	return null;
}
function reqMapping(value, what) {
	if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error(`floorhouse-card: ${what} must be a mapping, got ${JSON.stringify(value)}`);
	return value;
}
function reqString(record, key, what) {
	const value = record[key];
	if (typeof value !== "string" || value.length === 0) throw new Error(`floorhouse-card: ${what}: "${key}" must be a non-empty string`);
	return value;
}
function reqNumber(record, key, what) {
	const value = record[key];
	if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`floorhouse-card: ${what}: "${key}" must be a finite number`);
	return value;
}
function optNumber(record, key, what) {
	const value = record[key];
	if (value === void 0) return;
	if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`floorhouse-card: ${what}: "${key}" must be a finite number`);
	return value;
}
function parseSensors(raw, what) {
	if (raw === void 0) return;
	const record = reqMapping(raw, what);
	const sensors = {};
	let any = false;
	for (const key of CHIP_KINDS) {
		const value = record[key];
		if (value === void 0) continue;
		if (typeof value !== "string" || value.length === 0) throw new Error(`floorhouse-card: ${what}: "${key}" must be a non-empty entity id`);
		sensors[key] = value;
		any = true;
	}
	return any ? sensors : void 0;
}
function parseDoor(raw, what) {
	const record = reqMapping(raw, what);
	const rawWall = record["wall"];
	const wall = WALL_SIDES.find((side) => side === rawWall);
	if (wall === void 0) throw new Error(`floorhouse-card: ${what}: "wall" must be one of ${WALL_SIDES.join("|")}`);
	const at = reqNumber(record, "at", what);
	if (at < 0) throw new Error(`floorhouse-card: ${what}: "at" must be >= 0`);
	const size = optNumber(record, "size", what) ?? .8;
	if (size <= 0) throw new Error(`floorhouse-card: ${what}: "size" must be > 0`);
	return {
		wall,
		at,
		size
	};
}
function parseRoom(raw, what, seenIds) {
	const record = reqMapping(raw, what);
	const id = reqString(record, "id", what);
	if (seenIds.has(id)) throw new Error(`floorhouse-card: ${what}: duplicate room id "${id}"`);
	const name = reqString(record, "name", what);
	const x = reqNumber(record, "x", what);
	const y = reqNumber(record, "y", what);
	const w = reqNumber(record, "w", what);
	const h = reqNumber(record, "h", what);
	if (x < 0 || y < 0) throw new Error(`floorhouse-card: ${what}: x/y must be >= 0`);
	if (w < 1 || h < 1) throw new Error(`floorhouse-card: ${what}: room "${id}" must be at least 1×1 cells`);
	const room = {
		id,
		name,
		x,
		y,
		w,
		h
	};
	const areaHint = record["area_hint"];
	if (typeof areaHint === "string") room.area_hint = areaHint;
	const sensors = parseSensors(record["sensors"], `${what} sensors`);
	if (sensors !== void 0) room.sensors = sensors;
	const doorsRaw = record["doors"];
	if (doorsRaw !== void 0) {
		if (!Array.isArray(doorsRaw)) throw new Error(`floorhouse-card: ${what}: "doors" must be a list`);
		const doors = [];
		for (const [i, rawDoor] of doorsRaw.entries()) {
			const doorWhat = `${what}.doors[${i}]`;
			const door = parseDoor(rawDoor, doorWhat);
			const wallLength = door.wall === "top" || door.wall === "bottom" ? w : h;
			if (door.at + door.size > wallLength + EPS$1) throw new Error(`floorhouse-card: ${doorWhat}: door at ${door.at} + size ${door.size} exceeds wall length ${wallLength}`);
			doors.push(door);
		}
		room.doors = doors;
	}
	const slotsRaw = record["slots"];
	if (slotsRaw !== void 0) {
		if (!Array.isArray(slotsRaw)) throw new Error(`floorhouse-card: ${what}: "slots" must be a list`);
		const slots = [];
		for (const [i, rawSlot] of slotsRaw.entries()) {
			const slotWhat = `${what}.slots[${i}]`;
			const slot = parseSlot(rawSlot, slotWhat);
			const size = DEFAULT_SLOT_SIZE[slot.kind];
			const sw = slot.w ?? size.w;
			const sh = slot.h ?? size.h;
			const sx = slot.x ?? 0;
			const sy = slot.y ?? 0;
			if (sx < 0 || sy < 0 || sx + sw > w + EPS$1 || sy + sh > h + EPS$1) throw new Error(`floorhouse-card: ${slotWhat}: tile ${sx},${sy} ${sw}×${sh} does not fit room "${id}" ${w}×${h}`);
			slots.push(slot);
		}
		room.slots = slots;
	}
	seenIds.add(id);
	return room;
}
function parseSlot(raw, what) {
	const record = reqMapping(raw, what);
	const rawKind = record["kind"];
	const kind = SLOT_KINDS.find((valid) => valid === rawKind);
	if (kind === void 0) throw new Error(`floorhouse-card: ${what}: "kind" must be one of ${SLOT_KINDS.join("|")}`);
	const slot = { kind };
	const entity = record["entity"];
	if (entity !== void 0) {
		if (typeof entity !== "string" || entity.length === 0) throw new Error(`floorhouse-card: ${what}: "entity" must be a non-empty entity id`);
		slot.entity = entity;
	}
	const secondary = record["secondary"];
	if (secondary !== void 0) {
		if (typeof secondary !== "string" || secondary.length === 0) throw new Error(`floorhouse-card: ${what}: "secondary" must be a non-empty entity id`);
		slot.secondary = secondary;
	}
	const extras = record["extras"];
	if (extras !== void 0) {
		const extrasRecord = reqMapping(extras, `${what} extras`);
		const mapped = {};
		for (const [key, value] of Object.entries(extrasRecord)) {
			if (typeof value !== "string" || value.length === 0) throw new Error(`floorhouse-card: ${what} extras: "${key}" must be an entity id`);
			mapped[key] = value;
		}
		slot.extras = mapped;
	}
	for (const key of [
		"x",
		"y",
		"w",
		"h"
	]) {
		const value = optNumber(record, key, what);
		if (value !== void 0) {
			if (value < 0) throw new Error(`floorhouse-card: ${what}: "${key}" must be >= 0`);
			slot[key] = value;
		}
	}
	const link = record["link"];
	if (link !== void 0) {
		if (typeof link !== "string") throw new Error(`floorhouse-card: ${what}: "link" must be a string`);
		slot.link = link;
	}
	const card = record["card"];
	if (card !== void 0) {
		const cardRecord = reqMapping(card, `${what} card`);
		if (typeof cardRecord["type"] !== "string") throw new Error(`floorhouse-card: ${what} card: "type" is required`);
		slot.card = cardRecord;
	}
	const title = record["title"];
	if (typeof title === "string") slot.title = title;
	return slot;
}
function parseFurniture(raw, what) {
	const record = reqMapping(raw, what);
	const rawType = record["type"];
	const type = FURNITURE_TYPES.find((valid) => valid === rawType);
	if (type === void 0) throw new Error(`floorhouse-card: ${what}: "type" must be one of ${OBJECT_TYPES.join("|")}`);
	const x = reqNumber(record, "x", what);
	const y = reqNumber(record, "y", what);
	const w = reqNumber(record, "w", what);
	const h = reqNumber(record, "h", what);
	if (w <= 0 || h <= 0) throw new Error(`floorhouse-card: ${what}: w/h must be > 0`);
	const object = {
		type,
		x,
		y,
		w,
		h
	};
	const rot = optNumber(record, "rot", what);
	if (rot !== void 0) object.rot = rot;
	return object;
}
function parseLightObject(record, what) {
	const entityId = reqString(record, "entity_id", what);
	const rawFixture = record["fixture"];
	const fixture = LIGHT_FIXTURES.find((valid) => valid === rawFixture);
	if (fixture === void 0) throw new Error(`floorhouse-card: ${what}: "fixture" must be one of ${LIGHT_FIXTURES.join("|")}`);
	const light = {
		type: "light",
		entity_id: entityId,
		fixture,
		x: reqNumber(record, "x", what),
		y: reqNumber(record, "y", what)
	};
	const polylineRaw = record["polyline"];
	if (polylineRaw !== void 0) {
		if (!Array.isArray(polylineRaw) || polylineRaw.length < 2) throw new Error(`floorhouse-card: ${what}: "polyline" must be a list of at least two [x, y] points`);
		const polyline = [];
		for (const [i, rawPoint] of polylineRaw.entries()) {
			if (!Array.isArray(rawPoint) || rawPoint.length !== 2) throw new Error(`floorhouse-card: ${what}.polyline[${i}]: must be an [x, y] pair`);
			const px = rawPoint[0];
			const py = rawPoint[1];
			if (typeof px !== "number" || !Number.isFinite(px) || typeof py !== "number" || !Number.isFinite(py)) throw new Error(`floorhouse-card: ${what}.polyline[${i}]: coordinates must be finite numbers`);
			polyline.push([px, py]);
		}
		light.polyline = polyline;
	}
	if (fixture === "strip" && light.polyline === void 0) throw new Error(`floorhouse-card: ${what}: fixture "strip" requires a "polyline"`);
	return light;
}
function parseFloorObject(raw, what) {
	const record = reqMapping(raw, what);
	if (record["type"] === "light") return parseLightObject(record, what);
	return parseFurniture(record, what);
}
function parseFloor(raw, index, seenIds) {
	const what = `floors[${index}]`;
	const record = reqMapping(raw, what);
	const id = reqString(record, "id", what);
	if (seenIds.has(id)) throw new Error(`floorhouse-card: duplicate floor id "${id}"`);
	const name = reqString(record, "name", what);
	const roomsRaw = record["rooms"];
	if (!Array.isArray(roomsRaw) || roomsRaw.length === 0) throw new Error(`floorhouse-card: ${what}: "rooms" must be a non-empty list`);
	const roomIds = /* @__PURE__ */ new Set();
	const rooms = roomsRaw.map((rawRoom, i) => parseRoom(rawRoom, `${what}.rooms[${i}]`, roomIds));
	const overlap = roomsOverlapError(rooms);
	if (overlap !== null) throw new Error(overlap);
	const floor = {
		id,
		name,
		rooms
	};
	const objectsRaw = record["objects"];
	if (objectsRaw !== void 0) {
		if (!Array.isArray(objectsRaw)) throw new Error(`floorhouse-card: ${what}: "objects" must be a list`);
		floor.objects = objectsRaw.map((rawObject, i) => parseFloorObject(rawObject, `${what}.objects[${i}]`));
	}
	const placement = furniturePlacementError(floor);
	if (placement !== null) throw new Error(placement);
	seenIds.add(id);
	return floor;
}
function parseFloorhouseConfig(raw) {
	const record = reqMapping(raw, "config");
	if (record["type"] !== "floorhouse-card") throw new Error(`floorhouse-card: config "type" must be "floorhouse-card", got ${JSON.stringify(record["type"])}`);
	const config = {
		type: "floorhouse-card",
		meters_per_cell: DEFAULT_METERS_PER_CELL,
		cell_px: 56,
		climate_exclude: [...DEFAULT_CLIMATE_EXCLUDE],
		floors: structuredClone(DEFAULT_FLOORS)
	};
	const dockDefault = structuredClone(DEFAULT_DOCK);
	if (typeof record["title"] === "string") config.title = record["title"];
	const meters = optNumber(record, "meters_per_cell", "config");
	if (meters !== void 0) {
		if (meters <= 0) throw new Error(`floorhouse-card: meters_per_cell must be > 0, got ${meters}`);
		config.meters_per_cell = meters;
	}
	const cellPx = optNumber(record, "cell_px", "config");
	if (cellPx !== void 0) config.cell_px = clampCellPx(cellPx);
	const excludeRaw = record["climate_exclude"];
	if (excludeRaw !== void 0) {
		if (!Array.isArray(excludeRaw) || excludeRaw.some((p) => typeof p !== "string")) throw new Error("floorhouse-card: climate_exclude must be a list of regex strings");
		const patterns = excludeRaw;
		for (const pattern of patterns) try {
			new RegExp(pattern, "i");
		} catch {
			throw new Error(`floorhouse-card: climate_exclude: invalid regex ${JSON.stringify(pattern)}`);
		}
		config.climate_exclude = patterns;
	}
	const floorsRaw = record["floors"];
	if (floorsRaw !== void 0) {
		if (!Array.isArray(floorsRaw) || floorsRaw.length === 0) throw new Error("floorhouse-card: floors must be a non-empty list");
		const floorIds = /* @__PURE__ */ new Set();
		config.floors = floorsRaw.map((rawFloor, i) => parseFloor(rawFloor, i, floorIds));
	}
	const dockRaw = record["dock"];
	if (dockRaw !== void 0) {
		if (!Array.isArray(dockRaw)) throw new Error("floorhouse-card: dock must be a list");
		config.dock = dockRaw.map((rawSlot, i) => parseSlot(rawSlot, `dock[${i}]`));
	} else config.dock = dockDefault;
	return config;
}
// src/cards/compact.ts
/**
* @file Compact entity tiles (spec §3.5): 1–2 cell renderers for lights,
* media players (incl. the expanded AISIS speaker view), sensor+sparkline
* combos, plants with a bound soil-moisture sensor, 3D printers, network
* devices, vacuum, switch and climate — plus service payload helpers.
*
* Accessed via: src/render/floorplan.ts renders room slots, the dock and the
* drag ghost from renderSlotTile; payloads and pure decisions are exercised
* by unit tests against the recording mock.
*
* Assumptions: tiles are non-scrolling squares; every interaction goes
* through hass.callService with the payload built by the exported helpers.
* History for sparklines comes from attributes.history when the integration
* provides it, otherwise a deterministic mock series derived from the entity
* id (the demo has no websocket history). The AISIS expanded view opens in
* the shared popover (floorhouse-card) via handlers.onOpen.
*/
/** light.toggle payload. */
function tapLight(hass, entity) {
	hass.callService("light", "toggle", { entity_id: entity });
}
/** switch.toggle payload. */
function tapSwitch(hass, entity) {
	hass.callService("switch", "toggle", { entity_id: entity });
}
/** media_player.media_play_pause payload. */
function tapMedia(hass, entity) {
	hass.callService("media_player", "media_play_pause", { entity_id: entity });
}
/** media_player.volume_set payload (0..1 slider). */
function setMediaVolume(hass, entity, volume) {
	hass.callService("media_player", "volume_set", {
		entity_id: entity,
		volume_level: Math.min(1, Math.max(0, volume))
	});
}
/** notify service name for an AISIS media player (spec §2 notify entity). */
function notifyServiceFor(mediaEntity) {
	return `${mediaEntity.replace(/^media_player\./, "")}_notify`;
}
/** TTS payload → notify.<speaker>_notify. */
function sendTts(hass, mediaEntity, message) {
	hass.callService("notify", notifyServiceFor(mediaEntity), { message });
}
/** vacuum.* action payloads; "wet" maps to the mop fan speed when exposed. */
function vacuumAction(hass, entity, action) {
	if (action === "wet") hass.callService("vacuum", "set_fan_speed", {
		entity_id: entity,
		fan_speed: "mop"
	});
	else {
		const service = action === "home" ? "return_to_base" : action;
		hass.callService("vacuum", service, { entity_id: entity });
	}
}
/** Vacuum tiles render only when a vacuum.* entity exists (spec §3.6). */
function vacuumVisible(states) {
	return Object.keys(states).some((entityId) => entityId.startsWith("vacuum."));
}
/** Plant tiles render only with a bound moisture sensor (spec §3.5). */
function plantSlotReady(slot) {
	return slot.extras?.["moisture"] !== void 0;
}
/** Even-index downsample to at most maxPoints values. */
function downsample(values, maxPoints) {
	if (values.length <= maxPoints) return [...values];
	const step = (values.length - 1) / (maxPoints - 1);
	const out = [];
	for (let i = 0; i < maxPoints; i++) out.push(values[Math.round(i * step)] ?? 0);
	return out;
}
/** SVG polyline path for a value series in a w×h box. */
function sparklinePath(values, w, h, maxPoints = 24) {
	if (values.length < 2) return "";
	const points = downsample(values, maxPoints);
	const min = Math.min(...points);
	const span = Math.max(...points) - min || 1;
	return points.map((value, i) => {
		const x = i / (points.length - 1) * (w - 2) + 1;
		const y = h - 1 - (value - min) / span * (h - 2);
		return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
	}).join(" ");
}
function hashSeed(text) {
	let hash = 0;
	for (const ch of text) hash = (hash * 31 + ch.charCodeAt(0)) % 2147483647;
	return hash;
}
/**
* History series for a sensor: attributes.history when the integration
* provides numbers, else a deterministic mock series around the current
* state (stable per entity id — the demo has no websocket history).
*/
function entitySeries(entityId, entity) {
	const history = entity?.attributes["history"];
	if (Array.isArray(history) && history.every((v) => typeof v === "number")) return history.filter((v) => typeof v === "number");
	const state = Number.parseFloat(entity?.state ?? "");
	const base = Number.isFinite(state) ? state : 0;
	let seed = hashSeed(entityId) || 1;
	const series = [];
	for (let i = 0; i < 24; i++) {
		seed = seed * 48271 % 2147483647;
		series.push(base + (seed / 2147483647 - .5) * Math.max(1, Math.abs(base) * .3));
	}
	series.push(base);
	return series;
}
function friendly(entity, fallback) {
	const name = entity?.attributes["friendly_name"];
	return typeof name === "string" && name.length > 0 ? name : fallback;
}
function sensorValue(entity) {
	const unit = entity?.attributes["unit_of_measurement"];
	return {
		value: entity?.state ?? "—",
		unit: typeof unit === "string" ? unit : ""
	};
}
function playIcon(paused) {
	return paused ? w`<svg viewBox="0 0 24 24" class="ico"><path d="M8 5v14l11-7z" fill="currentColor" /></svg>` : w`<svg viewBox="0 0 24 24" class="ico"><path d="M6 5h4v14H6zM14 5h4v14h-4z" fill="currentColor" /></svg>`;
}
function bulbIcon(on) {
	return w`<svg viewBox="0 0 24 24" class="ico" style="color:${on ? "#ffd77a" : "#7d8794"}"><path d="M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.3 1 2.1h5c0-.8.4-1.6 1-2.1A6 6 0 0 0 12 3Z" fill="currentColor" opacity="${on ? .95 : .45}" /><path d="M9.5 18h5M10.5 20.5h3" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" /></svg>`;
}
/**
* One compact tile. Returns nothing for tiles that must stay hidden
* (plant without a moisture binding, vacuum with no vacuum.* entity).
*/
function renderSlotTile(slot, hass, source, handlers) {
	const states = hass?.states ?? {};
	const entity = slot.entity === void 0 ? void 0 : states[slot.entity];
	const secondaryEntity = slot.secondary === void 0 ? void 0 : states[slot.secondary];
	const down = (event) => {
		event.stopPropagation();
	};
	switch (slot.kind) {
		case "light":
			if (entity === void 0) return A;
			return b`
        <div
          class="tile kind-light${entity.state === "on" ? " on" : ""}"
          title=${friendly(entity, slot.entity ?? "")}
          @pointerdown=${(event) => handlers.onPointerDown(event, source, slot)}
          @click=${() => hass !== void 0 && slot.entity !== void 0 && tapLight(hass, slot.entity)}
        >
          ${bulbIcon(entity.state === "on")}
        </div>
      `;
		case "switch":
			if (entity === void 0) return A;
			return b`
        <div
          class="tile kind-switch${entity.state === "on" ? " on" : ""}"
          title=${friendly(entity, slot.entity ?? "")}
          @pointerdown=${(event) => handlers.onPointerDown(event, source, slot)}
          @click=${() => hass !== void 0 && slot.entity !== void 0 && tapSwitch(hass, slot.entity)}
        >
          <span class="sw-dot"></span><span class="sw-label">вкл</span>
        </div>
      `;
		case "media_player": {
			if (entity === void 0) return A;
			const paused = entity.state !== "playing";
			const volume = typeof entity.attributes["volume_level"] === "number" ? entity.attributes["volume_level"] : void 0;
			const aisis = (slot.entity ?? "").includes("aisis");
			return b`
        <div
          class="tile kind-media${entity.state === "playing" ? " on" : ""}"
          title=${friendly(entity, slot.entity ?? "")}
          @pointerdown=${(event) => handlers.onPointerDown(event, source, slot)}
        >
          <button
            class="tap"
            title="Пауза/воспроизведение"
            @pointerdown=${down}
            @click=${() => hass !== void 0 && slot.entity !== void 0 && tapMedia(hass, slot.entity)}
          >
            ${playIcon(paused)}
          </button>
          <span class="media-title"
            >${entity.attributes["media_title"] ?? friendly(entity, "")}</span
          >
          <input
            class="vol"
            type="range"
            min="0"
            max="1"
            step="0.05"
            .value=${String(volume ?? .5)}
            title="Громкость"
            @pointerdown=${down}
            @change=${(event) => {
				if (hass !== void 0 && slot.entity !== void 0) {
					const target = event.target;
					setMediaVolume(hass, slot.entity, Number.parseFloat(target.value));
				}
			}}
          />
          ${aisis ? b`<button
                  class="tap expand"
                  title="Подробнее"
                  @pointerdown=${down}
                  @click=${() => handlers.onOpen(slot)}
                >
                  ⤢
                </button>` : A}
        </div>
      `;
		}
		case "sensor": {
			const first = sensorValue(entity);
			const second = slot.secondary === void 0 ? null : sensorValue(secondaryEntity);
			const series = entitySeries(slot.entity ?? "", entity);
			return b`
        <div
          class="tile kind-sensor"
          title=${slot.title ?? friendly(entity, slot.entity ?? "")}
          @pointerdown=${(event) => handlers.onPointerDown(event, source, slot)}
        >
          <span class="val">${first.value}<small>${first.unit}</small></span>
          ${second === null ? A : b`<span class="val dim">${second.value}<small>${second.unit}</small></span>`}
          <svg class="spark" viewBox="0 0 48 14" preserveAspectRatio="none">
            <path d=${sparklinePath(series, 48, 14)} />
          </svg>
        </div>
      `;
		}
		case "plant": {
			if (!plantSlotReady(slot)) return A;
			const moistureEntity = states[slot.extras?.["moisture"] ?? ""];
			const moisture = Number.parseFloat(moistureEntity?.state ?? "");
			return b`
        <div
          class="tile kind-plant level-${Number.isFinite(moisture) ? moisture < 20 ? "dry" : moisture > 60 ? "wet" : "ok" : "none"}"
          title=${friendly(entity, slot.entity ?? "")}
          @pointerdown=${(event) => handlers.onPointerDown(event, source, slot)}
        >
          <svg viewBox="0 0 24 24" class="ico">
            <circle cx="12" cy="12" r="5" fill="none" stroke="currentColor" stroke-width="2" />
            <path d="M12 7c0-2.5 1.5-4 3.5-4.5C15.5 5 14.5 6.8 12 7Z" fill="currentColor" />
          </svg>
          ${Number.isFinite(moisture) ? b`<span class="badge">${Math.round(moisture)}%</span>` : A}
        </div>
      `;
		}
		case "printer": {
			const progress = typeof entity?.attributes["progress"] === "number" ? entity.attributes["progress"] : null;
			const nozzle = entity?.attributes["nozzle_temperature"];
			const bed = entity?.attributes["bed_temperature"];
			const eta = entity?.attributes["eta"];
			const circumference = 2 * Math.PI * 15;
			return b`
        <div
          class="tile kind-printer"
          title=${friendly(entity, slot.entity ?? "")}
          @pointerdown=${(event) => handlers.onPointerDown(event, source, slot)}
        >
          <svg viewBox="0 0 36 36" class="ring">
            <circle cx="18" cy="18" r="15" fill="none" stroke="#3a4250" stroke-width="4" />
            ${progress === null ? A : w`<circle cx="18" cy="18" r="15" fill="none" stroke="#6fb3e8" stroke-width="4" stroke-linecap="round" stroke-dasharray="${progress / 100 * circumference} ${circumference}" transform="rotate(-90 18 18)" />`}
            <text x="18" y="21" text-anchor="middle" class="ring-text">
              ${progress === null ? "—" : `${Math.round(progress)}%`}
            </text>
          </svg>
          <span class="p-meta">
            ${typeof nozzle === "number" ? b`<span>${Math.round(nozzle)}°</span>` : A}
            ${typeof bed === "number" ? b`<span>${Math.round(bed)}°</span>` : A}
            ${typeof eta === "string" ? b`<span class="dim">⏳ ${eta}</span>` : A}
          </span>
        </div>
      `;
		}
		case "network": return b`
        <a
          class="tile kind-network${entity?.state === "home" ? " on" : ""}"
          href=${slot.link ?? "/system-metrics"}
          title=${friendly(entity, slot.entity ?? "")}
          @pointerdown=${(event) => handlers.onPointerDown(event, source, slot)}
        >
          <span class="net-dot"></span><span class="net-label">${friendly(entity, "сеть")}</span>
        </a>
      `;
		case "vacuum": {
			if (!vacuumVisible(states)) return A;
			const fanList = entity?.attributes["fan_speed_list"];
			const wet = Array.isArray(fanList) && fanList.includes("mop");
			const battery = typeof entity?.attributes["battery_level"] === "number";
			const act = (action) => (event) => {
				event.stopPropagation();
				if (hass !== void 0 && slot.entity !== void 0) vacuumAction(hass, slot.entity, action);
			};
			return b`
        <div
          class="tile kind-vacuum"
          title=${friendly(entity, "Робот-пылесос")}
          @pointerdown=${(event) => handlers.onPointerDown(event, source, slot)}
        >
          <span class="vac-state"
            >${entity?.state ?? "—"}${battery ? b` · ${entity.attributes["battery_level"]}%` : A}</span
          >
          <span class="vac-actions">
            <button class="tap" title="Сухая уборка" @pointerdown=${down} @click=${act("start")}>
              ▶
            </button>
            ${wet ? b`<button class="tap" title="Влажная уборка" @pointerdown=${down} @click=${act("wet")}>💧</button>` : A}
            <button class="tap" title="Пауза" @pointerdown=${down} @click=${act("pause")}>⏸</button>
            <button class="tap" title="На базу" @pointerdown=${down} @click=${act("home")}>
              ⌂
            </button>
          </span>
        </div>
      `;
		}
		case "climate": {
			const current = entity?.attributes["current_temperature"];
			const target = entity?.attributes["temperature"];
			return b`
        <div
          class="tile kind-climate${entity?.state === "heat" ? " on" : ""}"
          title=${friendly(entity, slot.entity ?? "")}
          @pointerdown=${(event) => handlers.onPointerDown(event, source, slot)}
        >
          <span class="val">${typeof target === "number" ? `${target}°` : "—"}</span>
          <span class="val dim">${typeof current === "number" ? `${current}°` : ""}</span>
        </div>
      `;
		}
		case "embed": return b`
        <div
          class="tile kind-embed"
          title=${slot.title ?? "Карточка"}
          @pointerdown=${(event) => handlers.onPointerDown(event, source, slot)}
          @click=${() => handlers.onOpen(slot)}
        >
          <span class="embed-mark">{ }</span>
        </div>
      `;
	}
}
/** Popover body for the expanded AISIS speaker view (spec §3.5). */
function renderAisisExpanded(slot, hass) {
	const entity = hass?.states[slot.entity ?? ""];
	const connection = hass?.states[slot.extras?.["connection"] ?? ""];
	const sound = hass?.states[slot.extras?.["sound_level"] ?? ""];
	const soundValue = Number.parseFloat(sound?.state ?? "");
	const volume = typeof entity?.attributes["volume_level"] === "number" ? entity.attributes["volume_level"] : .5;
	return b`
    <div class="aisis">
      <div class="aisis-row">
        <span class="conn ${connection?.state === "on" ? "on" : "off"}"></span>
        ${connection?.state === "on" ? "подключён" : "нет связи"}
        <span class="aisis-title">${entity?.attributes["media_title"] ?? "—"}</span>
      </div>
      <div class="aisis-row">
        <button
          class="tap"
          @click=${() => hass !== void 0 && slot.entity !== void 0 && tapMedia(hass, slot.entity)}
        >
          ${playIcon(entity?.state !== "playing")}
        </button>
        <input
          class="vol"
          type="range"
          min="0"
          max="1"
          step="0.05"
          .value=${String(volume)}
          @change=${(event) => {
		if (hass !== void 0 && slot.entity !== void 0) {
			const target = event.target;
			setMediaVolume(hass, slot.entity, Number.parseFloat(target.value));
		}
	}}
        />
      </div>
      ${Number.isFinite(soundValue) ? b`
              <div class="gauge">
                <div class="gauge-bar">
                  <div
                    class="gauge-fill"
                    style="width:${Math.min(100, Math.max(0, soundValue))}%"
                  ></div>
                </div>
                <span>${sound?.state} ${sound?.attributes["unit_of_measurement"] ?? ""}</span>
              </div>
            ` : A}
      <textarea class="tts" rows="2" placeholder="Текст для Алисы…"></textarea>
      <button
        class="tap wide"
        @click=${(event) => {
		const area = event.target.closest(".aisis")?.querySelector(".tts");
		if (hass !== void 0 && slot.entity !== void 0 && area !== null && area !== void 0 && area.value.trim().length > 0) {
			sendTts(hass, slot.entity, area.value.trim());
			area.value = "";
		}
	}}
      >
        Произнести
      </button>
    </div>
  `;
}
/** Tile box in px for a slot at a cell size (kind defaults applied). */
function slotSize(slot, cell) {
	const size = DEFAULT_SLOT_SIZE[slot.kind];
	return {
		w: (slot.w ?? size.w) * cell,
		h: (slot.h ?? size.h) * cell
	};
}
// \0@oxc-project+runtime@0.156.0/helpers/esm/decorate.js
function __decorate(decorators, target, key, desc) {
	var c = arguments.length, r = c < 3 ? target : desc === null ? desc = Object.getOwnPropertyDescriptor(target, key) : desc, d;
	if (typeof Reflect === "object" && typeof Reflect.decorate === "function") r = Reflect.decorate(decorators, target, key, desc);
	else for (var i = decorators.length - 1; i >= 0; i--) if (d = decorators[i]) r = (c < 3 ? d(r) : c > 3 ? d(target, key, r) : d(target, key)) || r;
	return c > 3 && r && Object.defineProperty(target, key, r), r;
}
// src/cards/ha-embed.ts
/**
* @file Vendored minimal Lovelace embed (spec §3.5/§4): renders standard HA
* cards at native size inside the popover.
*
* Accessed via: FloorhouseCard's embed popover mounts <floorhouse-embed> with
* the slot's card config and the live hass object.
*
* Assumptions (reviewer findings, real-HA breakage): core cards are
* LAZY-LOADED — resolution must go through `await window.loadCardHelpers()`
* and helpers.createCardElement, never a customElements.get('hui-…') probe
* (on a dashboard hosting only floorhouse most hui types never register).
* The created element is cached per card-config hash, receives `element.hass`
* on every hass update (cards render blank without it), and is rebuilt on the
* card's own `ll-rebuild` event. The JSON fallback runs only where
* window.loadCardHelpers is undefined — the demo page. Live-HA verification
* of standard cards (gauge, media-control, history-graph) in the popover is
* deploy-scope ticket #108; the demo cannot exercise it.
*/
/** JSON dump fallback — demo environments without the Lovelace runtime. */
function renderEmbedFallback(card) {
	if (card === void 0 || typeof card["type"] !== "string") return b`<p class="embed-fallback">карточка не настроена</p>`;
	return b`<div class="embed-fallback">
    <p>Ловелace-рантайм недоступен в демо — карточка в натуральном размере появится в HA:</p>
    <pre>${JSON.stringify(card, null, 2)}</pre>
  </div>`;
}
/** One embedded Lovelace card; caches its element and forwards hass. */
var FloorhouseEmbed = class extends i {
	constructor(..._args) {
		super(..._args);
		this._element = null;
		this._key = "";
	}
	createRenderRoot() {
		return this;
	}
	render() {
		if (typeof window !== "undefined" && typeof window.loadCardHelpers !== "function") return renderEmbedFallback(this.card);
		return A;
	}
	async update(changed) {
		await super.update(changed);
		if (this.card === void 0) return;
		const key = JSON.stringify(this.card);
		if (key !== this._key || this._element === null) {
			const element = await this.createElement(this.card);
			if (element === null) return;
			this._key = key;
			this._element = element;
			this.replaceChildren(element);
		}
		this._element.hass = this.hass;
	}
	async createElement(card) {
		const loader = typeof window === "undefined" ? void 0 : window.loadCardHelpers;
		if (loader === void 0) return null;
		const element = (await loader()).createCardElement(card);
		element.addEventListener("ll-rebuild", () => {
			this._element?.remove();
			this._element = null;
			this._key = "";
			this.requestUpdate();
		});
		return element;
	}
	disconnectedCallback() {
		this._element?.remove();
		this._element = null;
		this._key = "";
		super.disconnectedCallback();
	}
};
__decorate([n({ attribute: false })], FloorhouseEmbed.prototype, "card", void 0);
__decorate([n({ attribute: false })], FloorhouseEmbed.prototype, "hass", void 0);
if (typeof customElements !== "undefined" && !customElements.get("floorhouse-embed")) customElements.define("floorhouse-embed", FloorhouseEmbed);
// src/header/clock.ts
/**
* @file Header clock widget (spec §3.1): big HH:MM ticking each minute plus
* the full localized (ru) date.
*
* Accessed via: src/header/header.ts drops <floorhouse-clock> into the
* widgets band; clockParts is exercised with fixed dates by unit tests.
*
* Assumptions: v1 taps do nothing; the element keeps its own 30s poll so the
* HH:MM flip never lags more than half a minute. Rendered in light DOM so the
* shared header styles (header.ts) apply.
*/
/** HH:MM + «четверг, 19 ноября 2026 г.» for a fixed moment (test seam). */
function clockParts(at) {
	const hours = String(at.getHours()).padStart(2, "0");
	const minutes = String(at.getMinutes()).padStart(2, "0");
	const date = new Intl.DateTimeFormat("ru-RU", {
		weekday: "long",
		day: "numeric",
		month: "long",
		year: "numeric"
	}).format(at);
	return {
		time: `${hours}:${minutes}`,
		date
	};
}
var FloorhouseClock = class extends i {
	constructor(..._args) {
		super(..._args);
		this._now = /* @__PURE__ */ new Date();
		this._timer = void 0;
	}
	connectedCallback() {
		super.connectedCallback();
		this._timer = window.setInterval(() => {
			this._now = /* @__PURE__ */ new Date();
		}, 3e4);
	}
	disconnectedCallback() {
		if (this._timer !== void 0) {
			clearInterval(this._timer);
			this._timer = void 0;
		}
		super.disconnectedCallback();
	}
	createRenderRoot() {
		return this;
	}
	render() {
		const parts = clockParts(this._now);
		return b`
      <div class="w-clock">
        <div class="time">${parts.time}</div>
        <div class="date">${parts.date}</div>
      </div>
    `;
	}
};
__decorate([r()], FloorhouseClock.prototype, "_now", void 0);
if (typeof customElements !== "undefined" && !customElements.get("floorhouse-clock")) customElements.define("floorhouse-clock", FloorhouseClock);
// src/header/events.ts
/**
* @file Header calendar widget (spec §3.1): the next events with time + title
* — rendered ONLY when calendar.* entities exist (the real home has none
* today, so the widget is absent there).
*
* Accessed via: src/header/header.ts composes it into the widgets band;
* calendarEntities/nextEvents/eventTimeLabel are exercised by unit tests
* against the calendar mock scenario.
*
* Assumptions: v1 reads events from calendar state attributes (the mock
* carries attributes.events); later waves may fetch via caldav/websocket.
* Events that started more than 30 minutes ago are dropped.
*/
/** Calendar entities in the states map, sorted by entity id for stability. */
function calendarEntities(states) {
	return Object.entries(states).filter(([entityId]) => entityId.startsWith("calendar.")).map(([, entity]) => entity).sort((a, b) => a.entity_id.localeCompare(b.entity_id));
}
/** Next `count` upcoming events across all calendars, soonest first. */
function nextEvents(entities, now, count = 3) {
	const events = [];
	for (const entity of entities) {
		const raw = entity.attributes["events"];
		if (!Array.isArray(raw)) continue;
		for (const entry of raw) {
			if (typeof entry !== "object" || entry === null) continue;
			const record = entry;
			const summary = record["summary"];
			const start = record["start"];
			if (typeof summary !== "string" || typeof start !== "string") continue;
			const at = new Date(start);
			if (Number.isNaN(at.getTime())) continue;
			events.push({
				summary,
				start: at
			});
		}
	}
	const cutoff = now.getTime() - 18e5;
	return events.filter((event) => event.start.getTime() >= cutoff).sort((a, b) => a.start.getTime() - b.start.getTime()).slice(0, count);
}
/** «18:00» same day, «20.11 10:30» otherwise. */
function eventTimeLabel(start, now) {
	if (start.getFullYear() === now.getFullYear() && start.getMonth() === now.getMonth() && start.getDate() === now.getDate()) return new Intl.DateTimeFormat("ru-RU", {
		hour: "2-digit",
		minute: "2-digit",
		hour12: false
	}).format(start);
	return new Intl.DateTimeFormat("ru-RU", {
		day: "2-digit",
		month: "2-digit",
		hour: "2-digit",
		minute: "2-digit",
		hour12: false
	}).format(start);
}
function renderEvents(hass, now) {
	const entities = calendarEntities(hass?.states ?? {});
	const events = nextEvents(entities, now);
	if (entities.length === 0) return A;
	return b`
    <div class="w-card w-events">
      <div class="todo-head">Календарь</div>
      ${events.length === 0 ? b`<div class="ev-row">
              <span class="ev-time">—</span><span>нет предстоящих событий</span>
            </div>` : events.map((event) => b`
                <div class="ev-row">
                  <span class="ev-time">${eventTimeLabel(event.start, now)}</span>
                  <span>${event.summary}</span>
                </div>
              `)}
    </div>
  `;
}
// src/header/todos.ts
/**
* @file Header todo widget (spec §3.1): every todo.* list as title + remaining
* count with its top unchecked items; tapping an item's checkbox completes it
* through hass.callService('todo', 'update_item').
*
* Accessed via: src/header/header.ts composes it into the widgets band;
* todoItems parsing and toggleTodoItem's service payload are exercised by
* unit tests against the recording mock.
*
* Assumptions: the HA service shape is todo/update_item with
* {entity_id, item: uid|summary, status: 'completed'} — uid preferred when the
* integration provides it (real HA accepts either). Items without a summary
* string are skipped defensively.
*/
/** Parsed items from a todo entity's attributes, garbage skipped. */
function todoItems(entity) {
	const raw = entity.attributes["items"];
	if (!Array.isArray(raw)) return [];
	const items = [];
	for (const entry of raw) {
		if (typeof entry !== "object" || entry === null) continue;
		const record = entry;
		const summary = record["summary"];
		const status = record["status"];
		if (typeof summary !== "string" || summary.length === 0 || typeof status !== "string") continue;
		const uid = record["uid"];
		items.push({
			summary,
			status,
			...typeof uid === "string" ? { uid } : {}
		});
	}
	return items;
}
/** Complete an item via todo/update_item (uid preferred over summary). */
function toggleTodoItem(hass, entityId, item) {
	hass.callService("todo", "update_item", {
		entity_id: entityId,
		item: item.uid ?? item.summary,
		status: "completed"
	});
}
function renderTodos(hass) {
	const lists = Object.entries(hass?.states ?? {}).filter(([entityId]) => entityId.startsWith("todo.")).map(([entityId, entity]) => ({
		entityId,
		entity
	}));
	if (lists.length === 0) return A;
	return b`
    <div class="w-card w-todos">
      ${lists.map(({ entityId, entity }) => {
		const uncheckedItems = todoItems(entity).filter((item) => item.status !== "completed");
		return b`
          <div class="todo-list">
            <div class="todo-head">
              ${entity.attributes["friendly_name"] ?? entityId}
              <span class="todo-count">${uncheckedItems.length}</span>
            </div>
            ${uncheckedItems.slice(0, 3).map((item) => b`
                  <button class="todo-item" @click=${() => toggleTodoItem(hass, entityId, item)}>
                    <span class="box"></span><span>${item.summary}</span>
                  </button>
                `)}
            ${uncheckedItems.length > 3 ? b`<div class="todo-more">и ещё ${uncheckedItems.length - 3}</div>` : A}
          </div>
        `;
	})}
    </div>
  `;
}
// src/render/geometry.ts
/**
* @file DOM-free grid geometry shared by the floorplan renderer and the glow
* canvas (kept here so render modules never import each other sideways).
*
* Accessed via: src/render/floorplan.ts (re-exports for its consumers),
* src/render/glow.ts, src/floorhouse-card.ts.
*/
/** Float tolerance for grid math shared across modules. */
var GRID_EPS = 1e-9;
/** Grid math: cells to CSS/SVG pixels. */
function cellsToPx$1(cells, cellPx) {
	return cells * cellPx;
}
/** Floor grid extent in cells — the bounding box of its rooms. */
function floorGridSize$1(floor) {
	let cols = 0;
	let rows = 0;
	for (const room of floor.rooms) {
		cols = Math.max(cols, room.x + room.w);
		rows = Math.max(rows, room.y + room.h);
	}
	return {
		cols,
		rows
	};
}
/**
* Effective cell size: the configured cell shrunk so `cols` cells fit the
* available container width (narrow/phone layouts — review finding: the fixed
* grid used to overflow the card). The 40–80 clamp stays the configured
* preference, but FIT WINS below it: at a 360px phone even the 40px floor
* (8×40=320) overflows the ~294px card, so the fitted value is used down to
* a 12px sanity bound. `available <= 0` means "not measured yet".
*/
function resolveCellPx(cellPx, available, cols) {
	if (available <= 0 || cols <= 0) return cellPx;
	const fitted = Math.min(80, Math.max(12, Math.floor(available / cols)));
	return Math.min(cellPx, fitted);
}
// src/render/glow.ts
/**
* @file Room light glow renderer (spec §3.3): one DPR-aware canvas per floor
* under the DOM card layer — additive ('lighter') radial gradients per light
* with fixture-scaled falloff, LED strips glowing along their polyline, and a
* subtle night-dim overlay on rooms whose lights are all off.
*
* Accessed via: src/render/floorplan.ts drops <floorhouse-glow> into each
* floor grid; pure color/geometry helpers are exercised by unit tests and
* paintGlow is proven in a real browser by scripts/screenshot.ts.
*
* Assumptions: color resolution order is rgb_color → color_temp_kelvin →
* color_temp (mireds) → warm white ~2700K; glow intensity scales with
* brightness 0..255 (absent = full). Canvas painting is imperative and runs
* after every Lit update; pointer-events stay on the DOM layers above.
*/
/** Falloff radius in grid cells per fixture kind (spec §3.3: radius by type). */
var FIXTURE_RADIUS_CELLS = {
	point: 1.9,
	ceiling: 3.4,
	strip: .75
};
/** Glow falloff radius in px for a fixture kind at the given cell size. */
function falloffRadiusPx(fixture, cellPx) {
	return FIXTURE_RADIUS_CELLS[fixture] * cellPx;
}
function clamp255(value) {
	return Math.min(255, Math.max(0, value));
}
/** Kelvin → RGB (Tanner Helland approximation, rounded to 0..255). */
function kelvinToRgb(kelvin) {
	const temp = kelvin / 100;
	let r;
	let g;
	let b;
	if (temp <= 66) {
		r = 255;
		g = 99.4708025861 * Math.log(temp) - 161.1195681661;
	} else {
		r = 329.698727446 * (temp - 60) ** -.1332047592;
		g = 288.1221695283 * (temp - 60) ** -.0755148492;
	}
	if (temp >= 66) b = 255;
	else if (temp <= 19) b = 0;
	else b = 138.5177312231 * Math.log(temp - 10) - 305.0447927307;
	return [
		Math.round(clamp255(r)),
		Math.round(clamp255(g)),
		Math.round(clamp255(b))
	];
}
/** Mireds (color_temp) → kelvin. */
function miredsToKelvin(mireds) {
	return 1e6 / mireds;
}
/** Warm-white fallback color, ~2700K. */
var WARM_WHITE = kelvinToRgb(2700);
function asTriple(value) {
	if (!Array.isArray(value) || value.length !== 3) return null;
	const [r, g, b] = value;
	if (typeof r !== "number" || typeof g !== "number" || typeof b !== "number" || !Number.isFinite(r + g + b)) return null;
	return [
		clamp255(r),
		clamp255(g),
		clamp255(b)
	];
}
/** Light color by precedence: rgb_color → kelvin → mireds → warm white. */
function resolveLightColor(attributes) {
	const rgb = asTriple(attributes["rgb_color"]);
	if (rgb !== null) return rgb;
	const kelvin = attributes["color_temp_kelvin"];
	if (typeof kelvin === "number" && Number.isFinite(kelvin) && kelvin > 0) return kelvinToRgb(kelvin);
	const mireds = attributes["color_temp"];
	if (typeof mireds === "number" && Number.isFinite(mireds) && mireds > 0) return kelvinToRgb(miredsToKelvin(mireds));
	return WARM_WHITE;
}
/** Glow intensity factor from brightness 0..255; absent attributes = full. */
function brightnessFactor(attributes) {
	const brightness = attributes["brightness"];
	if (typeof brightness !== "number" || !Number.isFinite(brightness)) return 1;
	return Math.min(1, Math.max(0, brightness / 255));
}
/** css rgb() string for a triple. */
function rgbCss([r, g, b]) {
	return `rgb(${r}, ${g}, ${b})`;
}
function rgbaCss([r, g, b], alpha) {
	return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
/** Room hit-test for lights: CLOSED intervals, so a fixture anchored exactly
* on a room's right/bottom edge still lights that room (the placement check
* accepts edge coordinates — review finding). First match wins on shared edges. */
function roomContainingLight(rooms, x, y) {
	for (const room of rooms) if (x >= room.x && x <= room.x + room.w && y >= room.y && y <= room.y + room.h) return room;
	return null;
}
function radialGlow(ctx, x, y, radius, color, intensity) {
	const alpha = .28 + .62 * intensity;
	const gradient = ctx.createRadialGradient(x, y, 0, x, y, radius);
	gradient.addColorStop(0, rgbaCss(color, alpha));
	gradient.addColorStop(.45, rgbaCss(color, alpha * .5));
	gradient.addColorStop(1, rgbaCss(color, 0));
	ctx.fillStyle = gradient;
	ctx.beginPath();
	ctx.arc(x, y, radius, 0, Math.PI * 2);
	ctx.fill();
}
function strokeStrip(ctx, polyline, cellPx, color, intensity) {
	if (polyline.length < 2) return;
	const width = falloffRadiusPx("strip", cellPx);
	ctx.save();
	ctx.lineCap = "round";
	ctx.lineJoin = "round";
	ctx.beginPath();
	const [firstX, firstY] = polyline[0];
	ctx.moveTo(firstX * cellPx, firstY * cellPx);
	for (const point of polyline.slice(1)) {
		const [px, py] = point;
		ctx.lineTo(px * cellPx, py * cellPx);
	}
	for (const [lineWidth, alpha] of [
		[width * 2.6, .14],
		[width * 1.5, .3],
		[width * .5, .85]
	]) {
		ctx.strokeStyle = rgbaCss(color, alpha * (.3 + .7 * intensity));
		ctx.lineWidth = lineWidth;
		ctx.stroke();
	}
	ctx.restore();
}
/**
* Paint one floor's glow canvas. Sizing is DPR-aware; the canvas is fully
* redrawn on every call (cheap at floor-grid size, keeps glow stateless).
*/
function paintGlow(canvas, floor, config, hass) {
	const grid = floorGridSize$1(floor);
	const cell = config.cell_px;
	const cssWidth = grid.cols * cell;
	const cssHeight = grid.rows * cell;
	const dpr = (canvas.ownerDocument?.defaultView ?? null)?.devicePixelRatio ?? 1;
	const pixelWidth = Math.max(1, Math.round(cssWidth * dpr));
	const pixelHeight = Math.max(1, Math.round(cssHeight * dpr));
	if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
	if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
	const ctx = canvas.getContext("2d");
	if (ctx === null) return;
	ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
	ctx.clearRect(0, 0, cssWidth, cssHeight);
	const lights = (floor.objects ?? []).filter(isLightObject);
	ctx.globalCompositeOperation = "source-over";
	for (const room of floor.rooms) if (!lights.filter((light) => roomContainingLight([room], light.x, light.y) !== null).some((light) => hass?.states[light.entity_id]?.state === "on")) {
		ctx.fillStyle = "rgba(4, 7, 18, 0.28)";
		ctx.fillRect(room.x * cell, room.y * cell, room.w * cell, room.h * cell);
	}
	ctx.globalCompositeOperation = "lighter";
	for (const light of lights) {
		const entity = hass?.states[light.entity_id];
		if (entity?.state !== "on") continue;
		const color = resolveLightColor(entity.attributes);
		const intensity = brightnessFactor(entity.attributes);
		const room = roomContainingLight(floor.rooms, light.x, light.y);
		ctx.save();
		if (room !== null) {
			ctx.beginPath();
			ctx.rect(room.x * cell, room.y * cell, room.w * cell, room.h * cell);
			ctx.clip();
		}
		if (light.fixture === "strip" && light.polyline !== void 0) strokeStrip(ctx, light.polyline, cell, color, intensity);
		else radialGlow(ctx, light.x * cell, light.y * cell, falloffRadiusPx(light.fixture, cell), color, intensity);
		ctx.restore();
	}
	ctx.globalCompositeOperation = "source-over";
}
/** One glow canvas per floor; light-DOM so card styles position it. */
var FloorhouseGlow = class extends i {
	createRenderRoot() {
		return this;
	}
	render() {
		return b`<canvas></canvas>`;
	}
	updated() {
		const canvas = this.querySelector("canvas");
		if (canvas instanceof HTMLCanvasElement && this.floor !== void 0 && this.config !== void 0) paintGlow(canvas, this.floor, this.config, this.hass);
	}
};
__decorate([n({ attribute: false })], FloorhouseGlow.prototype, "floor", void 0);
__decorate([n({ attribute: false })], FloorhouseGlow.prototype, "config", void 0);
__decorate([n({ attribute: false })], FloorhouseGlow.prototype, "hass", void 0);
if (typeof customElements !== "undefined" && !customElements.get("floorhouse-glow")) customElements.define("floorhouse-glow", FloorhouseGlow);
// src/render/sky.ts
/**
* @file Sky strip + page background (spec §3.3): sun/moon disc positioned by
* sun.sun elevation/azimuth on a simple arc, cloud/rain/snow/fog layers per
* weather condition, and CSS custom properties driving the page-level
* background gradient by daylight phase (night/dawn/day/dusk) — darkened for
* overcast/rain/snow/fog.
*
* Accessed via: FloorhouseCard.render puts the strip at the top and applies
* skyVars() to the card root; pure phase/color math is exercised by unit
* tests, layout by scripts/screenshot.ts.
*
* Assumptions: phase boundaries sit at sun elevations -6° (night edge) and
* +6° (full day), with -0.5° (sun disc edge) splitting twilight into a deep
* and a bright band used for gradient strength; dawn vs dusk comes from the
* sun.sun `rising` attribute (HA exposes it directly). Missing entities fall
* back to a neutral partly-cloudy day so the card never renders a black void.
*/
var SUN_ENTITY = "sun.sun";
var WEATHER_ENTITY = "weather.forecast_home_assistant";
/** Phase from sun elevation; `rising` picks dawn vs dusk in the twilight band. */
function daylightPhase(elevation, rising = true) {
	if (elevation < -6) return "night";
	if (elevation < 6) return rising ? "dawn" : "dusk";
	return "day";
}
/** Twilight band: -6..-0.5 is deep, -0.5..6 is bright (used for gradient strength). */
function twilightBand(elevation) {
	return elevation < -.5 ? "deep" : "bright";
}
/** How much weather darkens the background, by HA's canonical conditions. */
function weatherDarken(condition) {
	switch (condition) {
		case "sunny":
		case "clear-night": return 0;
		case "windy":
		case "windy-variant": return .1;
		case "partlycloudy": return .15;
		case "snowy": return .3;
		case "cloudy": return .35;
		case "fog": return .4;
		case "snowy-rainy":
		case "rainy": return .45;
		case "exceptional":
		case "hail": return .5;
		case "pouring":
		case "lightning":
		case "lightning-rainy": return .55;
		default: return .2;
	}
}
var DARK_TARGET = [
	13,
	17,
	23
];
function mix(from, toward, amount) {
	return [
		Math.round(from[0] + (toward[0] - from[0]) * amount),
		Math.round(from[1] + (toward[1] - from[1]) * amount),
		Math.round(from[2] + (toward[2] - from[2]) * amount)
	];
}
var PHASE_BASE = {
	day: {
		skyA: [
			126,
			179,
			232
		],
		skyB: [
			185,
			215,
			242
		],
		bgA: [
			46,
			65,
			82
		],
		bgB: [
			27,
			40,
			56
		]
	},
	dawn: {
		skyA: [
			242,
			166,
			90
		],
		skyB: [
			122,
			107,
			184
		],
		bgA: [
			58,
			48,
			80
		],
		bgB: [
			35,
			32,
			50
		]
	},
	dusk: {
		skyA: [
			232,
			112,
			90
		],
		skyB: [
			107,
			74,
			122
		],
		bgA: [
			64,
			46,
			66
		],
		bgB: [
			36,
			29,
			44
		]
	},
	night: {
		skyA: [
			16,
			22,
			43
		],
		skyB: [
			6,
			9,
			19
		],
		bgA: [
			12,
			17,
			31
		],
		bgB: [
			7,
			10,
			18
		]
	}
};
var DAY_SKY = PHASE_BASE.day.skyA;
/** CSS colors for the sky strip and page background, weather-darkened; the
* twilight band drives gradient strength — deep twilight keeps the darker sky,
* bright twilight leans toward the day sky (review cleanup: was dead). */
function phaseColors(phase, darken, band = "bright") {
	const base = PHASE_BASE[phase];
	const d = Math.min(1, Math.max(0, darken));
	const lift = phase !== "day" && phase !== "night" && band === "bright" ? .35 : 0;
	return {
		skyA: rgbCss(mix(mix(base.skyA, DAY_SKY, lift), DARK_TARGET, d * .8)),
		skyB: rgbCss(mix(mix(base.skyB, DAY_SKY, lift), DARK_TARGET, d * .8)),
		bgA: rgbCss(mix(base.bgA, DARK_TARGET, d)),
		bgB: rgbCss(mix(base.bgB, DARK_TARGET, d))
	};
}
/** Read sun + weather into one sky state, with neutral day defaults. */
function readSkyState(hass) {
	const sun = hass?.states[SUN_ENTITY];
	const weather = hass?.states[WEATHER_ENTITY];
	const attributes = sun?.attributes ?? {};
	const elevationRaw = attributes["elevation"];
	const azimuthRaw = attributes["azimuth"];
	const elevation = typeof elevationRaw === "number" && Number.isFinite(elevationRaw) ? elevationRaw : 25;
	const azimuth = typeof azimuthRaw === "number" && Number.isFinite(azimuthRaw) ? azimuthRaw : 200;
	const risingAttr = attributes["rising"];
	return {
		elevation,
		azimuth,
		rising: typeof risingAttr === "boolean" ? risingAttr : true,
		condition: weather?.state ?? "partlycloudy"
	};
}
/** Sun/moon disc position on the sky arc; width may be 100 (%). */
function celestialPosition(elevation, azimuth, width, height) {
	const xFrac = Math.min(1, Math.max(0, (azimuth - 60) / 240));
	const yFrac = Math.min(1, Math.max(0, (elevation + 6) / 60));
	const horizon = height * .8;
	return {
		x: 4 + xFrac * (width - 8),
		y: horizon - yFrac * (horizon - 10),
		aboveHorizon: elevation > -.5
	};
}
var FOUR_CLOUDS = [
	"cloudy",
	"rainy",
	"pouring",
	"snowy",
	"snowy-rainy",
	"hail",
	"lightning",
	"lightning-rainy",
	"exceptional"
];
var TWO_CLOUDS = [
	"partlycloudy",
	"windy",
	"windy-variant"
];
var RAINY = [
	"rainy",
	"pouring",
	"lightning",
	"lightning-rainy",
	"snowy-rainy",
	"hail"
];
function cloudEls(condition) {
	const count = FOUR_CLOUDS.includes(condition) ? 4 : TWO_CLOUDS.includes(condition) ? 2 : 0;
	return [
		"c1",
		"c2",
		"c3",
		"c4"
	].slice(0, count).map((cls) => b`<div class="cloud ${cls}"></div>`);
}
/** Rain and/or snow/fog layers; snowy-rainy renders both rain and snow. */
function precipEls(condition) {
	const layers = [];
	if (RAINY.includes(condition)) layers.push(b`<div class="rain"></div>`);
	if (condition === "snowy" || condition === "snowy-rainy") layers.push(b`<div class="snow"></div>`);
	if (condition === "fog") layers.push(b`<div class="fog"></div>`);
	return layers;
}
/** The sky strip element — sun or moon disc, clouds, precipitation. */
function renderSky(hass) {
	const sky = readSkyState(hass);
	const phase = daylightPhase(sky.elevation, sky.rising);
	const pos = celestialPosition(sky.elevation, sky.azimuth, 100, 72);
	const disc = pos.aboveHorizon ? "sun" : "moon";
	return b`
    <div class="sky phase-${phase}" data-condition="${sky.condition}">
      ${cloudEls(sky.condition)} ${precipEls(sky.condition).map((layer) => layer)}
      <div class="${disc}" style="left:${pos.x}%;top:${pos.y}px"></div>
    </div>
  `;
}
/** Inline style value setting the card's sky/background CSS variables. */
function skyVars(hass) {
	const sky = readSkyState(hass);
	const colors = phaseColors(daylightPhase(sky.elevation, sky.rising), weatherDarken(sky.condition), twilightBand(sky.elevation));
	return [
		`--fh-sky-a:${colors.skyA}`,
		`--fh-sky-b:${colors.skyB}`,
		`--fh-bg-a:${colors.bgA}`,
		`--fh-bg-b:${colors.bgB}`
	].join(";");
}
/** Styles for the sky strip; FloorhouseCard concatenates them with its shell. */
var skyStyles = i$3`
  .sky {
    position: relative;
    height: ${72}px;
    border-radius: 10px;
    overflow: hidden;
    margin-bottom: 14px;
    background: linear-gradient(180deg, var(--fh-sky-a, #2e4152), var(--fh-sky-b, #1b2838));
  }
  .sky .sun {
    position: absolute;
    width: 30px;
    height: 30px;
    margin: -15px 0 0 -15px;
    border-radius: 50%;
    background: radial-gradient(circle at 35% 35%, #fff3c4, #ffc94d 62%, #f5a623);
    box-shadow: 0 0 24px 7px rgba(255, 200, 90, 0.55);
  }
  .sky .moon {
    position: absolute;
    width: 22px;
    height: 22px;
    margin: -11px 0 0 -11px;
    border-radius: 50%;
    background: radial-gradient(circle at 35% 35%, #ffffff, #dfe6f2 55%, #b9c4d8);
    box-shadow: 0 0 18px 5px rgba(190, 205, 235, 0.4);
  }
  .sky .cloud {
    position: absolute;
    border-radius: 999px;
    background: rgba(233, 239, 247, 0.78);
    filter: blur(2px);
  }
  .sky .cloud.c1 {
    width: 70px;
    height: 15px;
    left: 12%;
    top: 16px;
  }
  .sky .cloud.c2 {
    width: 52px;
    height: 12px;
    left: 64%;
    top: 32px;
    opacity: 0.85;
  }
  .sky .cloud.c3 {
    width: 88px;
    height: 16px;
    left: 38%;
    top: 10px;
    opacity: 0.95;
  }
  .sky .cloud.c4 {
    width: 60px;
    height: 13px;
    left: 74%;
    top: 14px;
  }
  .sky .rain {
    position: absolute;
    inset: 26px 0 0 0;
    background: repeating-linear-gradient(
      76deg,
      transparent 0 13px,
      rgba(165, 195, 235, 0.38) 13px 15px
    );
  }
  .sky .snow {
    position: absolute;
    inset: 20px 0 0 0;
    background-image: radial-gradient(circle, rgba(255, 255, 255, 0.85) 1.6px, transparent 2.2px);
    background-size: 23px 19px;
  }
  .sky .fog {
    position: absolute;
    inset: auto 0 0 0;
    height: 34px;
    background: linear-gradient(180deg, transparent, rgba(205, 214, 224, 0.4));
  }
`;
// src/header/weather.ts
/**
* @file Header weather widget (spec §3.1): nowcast card (condition icon,
* current temp, feels-like, humidity/wind chips) plus a row of forecast
* chips — the next 4 entries from the weather entity's forecast attribute,
* daily or hourly.
*
* Accessed via: src/header/header.ts composes it into the widgets band;
* forecastChips derivation is exercised by unit tests.
*
* Assumptions: entries with a numeric templow are daily (label = weekday),
* entries without are hourly (label = HH:00); the timestamp lives in
* `datetime` (newer HA) or legacy `time`. A missing/garbage forecast simply
* yields no chips — the nowcast card still renders.
*/
function parseEntries(rawForecast) {
	if (!Array.isArray(rawForecast)) return [];
	const entries = [];
	for (const raw of rawForecast) {
		if (typeof raw !== "object" || raw === null) continue;
		const record = raw;
		const condition = record["condition"];
		const temperature = record["temperature"];
		const templow = record["templow"];
		const stamp = record["datetime"] ?? record["time"];
		if (typeof condition !== "string" || typeof temperature !== "number" || typeof stamp !== "string") continue;
		const at = new Date(stamp);
		if (Number.isNaN(at.getTime())) continue;
		entries.push({
			condition,
			temperature,
			...typeof templow === "number" ? { templow } : {},
			at
		});
	}
	return entries;
}
/** Up to 4 forecast chips; daily entries get weekday + hi/lo, hourly get HH:00. */
function forecastChips(rawForecast) {
	const entries = parseEntries(rawForecast).slice(0, 4);
	if (entries.length === 0) return [];
	const daily = entries[0]?.templow !== void 0;
	return entries.map((entry) => {
		return {
			label: daily ? new Intl.DateTimeFormat("ru-RU", { weekday: "short" }).format(entry.at) : new Intl.DateTimeFormat("ru-RU", {
				hour: "2-digit",
				minute: "2-digit",
				hour12: false
			}).format(entry.at),
			condition: entry.condition,
			hi: entry.temperature,
			...entry.templow !== void 0 ? { lo: entry.templow } : {}
		};
	});
}
function num(value) {
	return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function conditionGlyph(condition) {
	const common = {
		fill: "none",
		stroke: "currentColor",
		"stroke-width": "1.6",
		"stroke-linecap": "round"
	};
	switch (condition) {
		case "sunny": return w`<svg viewBox="0 0 24 24" class="cond-icon" aria-hidden="true"><circle cx="12" cy="12" r="4" ${common} /><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4" ${common} /></svg>`;
		case "clear-night": return w`<svg viewBox="0 0 24 24" class="cond-icon" aria-hidden="true"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5Z" ${common} /></svg>`;
		case "partlycloudy": return w`<svg viewBox="0 0 24 24" class="cond-icon" aria-hidden="true"><circle cx="8" cy="8" r="2.6" ${common} /><path d="M6 8v0M8 3.5v0M3.5 8h0" ${common} /><path d="M9 18h8a3 3 0 0 0 .5-5.96A4.5 4.5 0 0 0 9 13.5 3 3 0 0 0 9 18Z" ${common} /></svg>`;
		case "rainy":
		case "pouring":
		case "lightning":
		case "lightning-rainy":
		case "snowy":
		case "snowy-rainy":
		case "hail": return w`<svg viewBox="0 0 24 24" class="cond-icon" aria-hidden="true"><path d="M7 16h9a3.5 3.5 0 0 0 .6-6.95A5 5 0 0 0 7 11a3 3 0 0 0 0 5Z" ${common} />${condition === "snowy" || condition === "snowy-rainy" ? w`<path d="M9 19.5h0M13 19.5h0M17 19.5h0" ${common} />` : w`<path d="M9 19l-1 2.5M13 19l-1 2.5M17 19l-1 2.5" ${common} />`}</svg>`;
		case "fog": return w`<svg viewBox="0 0 24 24" class="cond-icon" aria-hidden="true"><path d="M4 9h16M6 13h14M4 17h12" ${common} /></svg>`;
		case "windy":
		case "windy-variant": return w`<svg viewBox="0 0 24 24" class="cond-icon" aria-hidden="true"><path d="M3 8h11a2.5 2.5 0 1 0-2.5-2.5M3 13h15a2.5 2.5 0 1 1-2.5 2.5M3 18h9" ${common} /></svg>`;
		default: return w`<svg viewBox="0 0 24 24" class="cond-icon" aria-hidden="true"><path d="M7 17h9a3.5 3.5 0 0 0 .6-6.95A5 5 0 0 0 7.6 11 3 3 0 0 0 7 17Z" ${common} /></svg>`;
	}
}
function renderWeather(hass) {
	const entity = hass?.states[WEATHER_ENTITY];
	if (entity === void 0) return A;
	const attributes = entity.attributes;
	const temperature = num(attributes["temperature"]);
	const feelsLike = num(attributes["apparent_temperature"]);
	const humidity = num(attributes["humidity"]);
	const windSpeed = num(attributes["wind_speed"]);
	const windUnit = typeof attributes["wind_speed_unit"] === "string" ? attributes["wind_speed_unit"] : "";
	const chips = forecastChips(attributes["forecast"]);
	return b`
    <div class="w-card w-weather">
      <div class="now">
        <span class="cond">${conditionGlyph(entity.state)}</span>
        ${temperature === null ? A : b`<span class="temp">${Math.round(temperature * 10) / 10}°</span>`}
        ${feelsLike === null ? A : b`<span class="feels">ощущается ${Math.round(feelsLike * 10) / 10}°</span>`}
        ${humidity === null ? A : b`<span class="pill">${Math.round(humidity)}%</span>`}
        ${windSpeed === null ? A : b`<span class="pill">ветер ${Math.round(windSpeed * 10) / 10} ${windUnit}</span>`}
      </div>
      ${chips.length === 0 ? A : b`
              <div class="fc-row">
                ${chips.map((chip) => b`
                    <span class="fc-chip">
                      ${conditionGlyph(chip.condition)}<span>${chip.label}</span>
                      <span>${Math.round(chip.hi)}°</span>
                      ${chip.lo === void 0 ? A : b`<span class="lo">${Math.round(chip.lo)}°</span>`}
                    </span>
                  `)}
              </div>
            `}
    </div>
  `;
}
// src/header/header.ts
/**
* @file Header band composer (spec §3.1): rectangular widget cards wrapping
* horizontally above the floors — clock, weather nowcast + forecast chips,
* todo lists and (when calendars exist) upcoming events.
*
* Accessed via: FloorhouseCard.render — the band sits between the sky strip
* and the floors; headerStyles is concatenated into the card's styles.
*
* Assumptions: widgets render in light DOM inside the card's shadow root so
* these shared styles apply; the clock keeps its own ticking state, the rest
* are pure functions of hass.
*/
function renderHeaderBand(hass) {
	return b`
    <div class="header-band">
      <floorhouse-clock class="w-card"></floorhouse-clock>
      ${renderWeather(hass)} ${renderTodos(hass)} ${renderEvents(hass, /* @__PURE__ */ new Date())}
    </div>
  `;
}
var headerStyles = i$3`
  .header-band {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 14px;
    align-items: stretch;
  }
  .w-card {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 10px;
    padding: 10px 14px;
    min-height: 64px;
    max-height: 96px;
    overflow: hidden;
  }
  .w-clock .time {
    font-size: 30px;
    font-weight: 650;
    letter-spacing: 0.02em;
    line-height: 1.15;
    font-variant-numeric: tabular-nums;
  }
  .w-clock .date {
    font-size: 12px;
    color: #a9b4c0;
    margin-top: 2px;
    white-space: nowrap;
  }
  .w-weather {
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 240px;
  }
  .w-weather .now {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .w-weather .temp {
    font-size: 24px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .w-weather .feels {
    font-size: 12px;
    color: #a9b4c0;
    white-space: nowrap;
  }
  .pill {
    font-size: 11px;
    background: rgba(255, 255, 255, 0.06);
    border-radius: 999px;
    padding: 1px 8px;
    color: #cdd6df;
    white-space: nowrap;
  }
  .cond {
    display: inline-flex;
    color: #ffd77a;
  }
  .cond-icon {
    width: 20px;
    height: 20px;
    display: block;
  }
  .fc-chip .cond-icon {
    width: 14px;
    height: 14px;
  }
  .fc-row {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .fc-chip {
    display: inline-flex;
    gap: 5px;
    align-items: center;
    font-size: 11px;
    color: #cdd6df;
    background: rgba(255, 255, 255, 0.05);
    border-radius: 8px;
    padding: 2px 7px;
    font-variant-numeric: tabular-nums;
  }
  .fc-chip .lo {
    color: #93a0ac;
  }
  .w-todos {
    display: flex;
    gap: 18px;
  }
  .todo-head {
    display: flex;
    gap: 6px;
    align-items: center;
    font-size: 12px;
    font-weight: 600;
    color: #cdd6df;
    margin-bottom: 4px;
    white-space: nowrap;
  }
  .todo-count {
    background: rgba(255, 255, 255, 0.08);
    border-radius: 999px;
    padding: 0 7px;
    font-size: 10px;
    color: #a9b4c0;
  }
  .todo-item {
    display: flex;
    gap: 7px;
    align-items: center;
    background: none;
    border: 0;
    color: #dfe5ea;
    font: inherit;
    font-size: 12px;
    padding: 2px 0;
    cursor: pointer;
    text-align: left;
  }
  .todo-item .box {
    width: 12px;
    height: 12px;
    border: 1.5px solid #8fa0af;
    border-radius: 3px;
    flex: none;
  }
  .todo-item:hover .box {
    background: rgba(255, 255, 255, 0.2);
  }
  .todo-more {
    font-size: 11px;
    color: #93a0ac;
  }
  .w-events {
    min-width: 200px;
  }
  .ev-row {
    display: flex;
    gap: 8px;
    font-size: 12px;
    padding: 2px 0;
    color: #dfe5ea;
    white-space: nowrap;
  }
  .ev-time {
    color: #a9b4c0;
    font-variant-numeric: tabular-nums;
    flex: none;
  }
`;
// src/render/dnd.ts
/** Dock placement: rows below the floors (wide) or leading column (narrow). */
function dockPlacement(containerWidth) {
	return resolveLayoutDirection(containerWidth) === "row" ? "below" : "leading";
}
/** Point → drop target: a room cell, the dock (grid miss or floor void), or null off-card. */
function dropTargetAt(grids, point) {
	for (const grid of grids) {
		if (!(point.x >= grid.left && point.x < grid.left + grid.width && point.y >= grid.top && point.y < grid.top + grid.height)) continue;
		const cellX = Math.round((point.x - grid.left) / grid.cell);
		const cellY = Math.round((point.y - grid.top) / grid.cell);
		for (const room of grid.rooms) if (cellX >= room.x && cellX <= room.x + room.w && cellY >= room.y && cellY <= room.y + room.h) return {
			type: "room",
			floorId: grid.floorId,
			roomId: room.id,
			x: Math.max(0, cellX - room.x),
			y: Math.max(0, cellY - room.y)
		};
		return { type: "dock" };
	}
	return null;
}
function cloneConfig(config) {
	return {
		...config,
		floors: config.floors.map((floor) => ({
			...floor,
			rooms: floor.rooms.map((room) => ({
				...room,
				...room.slots === void 0 ? {} : { slots: room.slots.map((slot) => ({ ...slot })) }
			}))
		})),
		...config.dock === void 0 ? {} : { dock: config.dock.map((slot) => ({ ...slot })) }
	};
}
/**
* Move a slot from its origin to a drop target in a NEW config (the input
* config is never mutated — the card replaces its state with the result).
*/
function applySlotDrop(config, origin, target) {
	const next = cloneConfig(config);
	let moved;
	if (origin.dockIndex !== void 0) moved = next.dock?.splice(origin.dockIndex, 1)[0];
	else if (origin.roomId !== void 0 && origin.roomIndex !== void 0) for (const floor of next.floors) {
		if (floor.id !== origin.floorId) continue;
		moved = floor.rooms.find((candidate) => candidate.id === origin.roomId)?.slots?.splice(origin.roomIndex, 1)[0];
	}
	if (moved === void 0) return config;
	if (target.type === "room") {
		const room = next.floors.find((candidate) => candidate.id === target.floorId)?.rooms.find((candidate) => candidate.id === target.roomId);
		if (room === void 0) {
			(next.dock ??= []).push(moved);
			return next;
		}
		moved.x = target.x;
		moved.y = target.y;
		(room.slots ??= []).push(moved);
		return next;
	}
	delete moved.x;
	delete moved.y;
	(next.dock ??= []).push(moved);
	return next;
}
// src/render/furniture.ts
/**
* @file Beige top-view furniture shape library — one inline SVG silhouette
* per FurnitureType (spec §3.2: couch incl. L, bed, table, chairs, plants,
* TV, desk, kitchen counter, stairs).
*
* Accessed via: src/render/floorplan.ts places each shape in an absolutely
* positioned box sized from the object's cell footprint × cell_px.
*
* Assumptions: purely decorative — low-contrast beige fills, no interaction,
* rendered under room labels and cards. Every shape draws into a viewBox that
* equals its pixel box, so geometry scales 1:1 with the grid cell size.
*/
var FILL = "#b5a485";
var DARK = "#86765a";
var LIGHT = "#cfc3a8";
/** Full <svg> element for one furniture object at the given pixel size. */
function furnitureSvg(type, w, h) {
	const shape = SHAPES[type];
	return b`
    <svg
      width="100%"
      height="100%"
      viewBox="0 0 ${w} ${h}"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      ${shape(w, h)}
    </svg>
  `;
}
var SHAPES = {
	couch: couchShape,
	"couch-l": couchLShape,
	bed: bedShape,
	table: tableShape,
	chair: chairShape,
	plant: plantShape,
	tv: tvShape,
	desk: deskShape,
	counter: counterShape,
	stairs: stairsShape
};
function couchShape(w$1, h) {
	const back = Math.max(4, h * .24);
	return w`
    <rect x="0.75" y="0.75" width="${w$1 - 1.5}" height="${h - 1.5}" rx="5" fill="${FILL}" stroke="${DARK}" stroke-width="1" />
    <rect x="3" y="3" width="${w$1 - 6}" height="${back}" rx="2.5" fill="${DARK}" opacity="0.55" />
    <rect x="4.5" y="${back + 5}" width="${w$1 - 9}" height="${Math.max(2, h - back - 10)}" rx="4" fill="${LIGHT}" opacity="0.75" />
  `;
}
function couchLShape(w$2, h) {
	const aw = w$2 * .42;
	const ah = h * .42;
	return w`
    <rect x="0.75" y="0.75" width="${aw - .75}" height="${h - 1.5}" rx="5" fill="${FILL}" stroke="${DARK}" stroke-width="1" />
    <rect x="0.75" y="${h - ah - .75}" width="${w$2 - 1.5}" height="${ah - .75}" rx="5" fill="${FILL}" stroke="${DARK}" stroke-width="1" />
    <rect x="2" y="2" width="4.5" height="${h - 4}" rx="2" fill="${DARK}" opacity="0.5" />
    <rect x="2" y="${h - 6.5}" width="${w$2 - 4}" height="4.5" rx="2" fill="${DARK}" opacity="0.5" />
    <rect x="8" y="5" width="${Math.max(2, aw - 12)}" height="${Math.max(2, h - ah - 10)}" rx="3" fill="${LIGHT}" opacity="0.7" />
    <rect x="${aw + 5}" y="${h - ah + 5}" width="${Math.max(2, w$2 - aw - 10)}" height="${Math.max(2, ah - 10)}" rx="3" fill="${LIGHT}" opacity="0.7" />
  `;
}
function bedShape(w$3, h) {
	const head = Math.max(3, h * .08);
	return w`
    <rect x="0.75" y="0.75" width="${w$3 - 1.5}" height="${h - 1.5}" rx="4" fill="${FILL}" stroke="${DARK}" stroke-width="1" />
    <rect x="1.5" y="1.5" width="${w$3 - 3}" height="${head}" rx="2" fill="${DARK}" opacity="0.6" />
    <rect x="${w$3 * .06}" y="${head + 3}" width="${w$3 * .38}" height="${h * .14}" rx="3" fill="${LIGHT}" />
    <rect x="${w$3 * .56}" y="${head + 3}" width="${w$3 * .38}" height="${h * .14}" rx="3" fill="${LIGHT}" />
    <line x1="2" y1="${h * .58}" x2="${w$3 - 2}" y2="${h * .58}" stroke="${DARK}" stroke-width="1" opacity="0.6" />
  `;
}
function tableShape(w$4, h) {
	const r = Math.min(w$4, h) * .24;
	return w`
    <rect x="0.75" y="0.75" width="${w$4 - 1.5}" height="${h - 1.5}" rx="${r}" fill="${FILL}" stroke="${DARK}" stroke-width="1" />
    <rect x="3.5" y="3.5" width="${w$4 - 7}" height="${h - 7}" rx="${Math.max(1, r - 3)}" fill="none" stroke="${DARK}" stroke-width="1" opacity="0.5" />
  `;
}
function chairShape(w$5, h) {
	const back = Math.max(2.5, h * .16);
	return w`
    <rect x="0.75" y="${back + 1}" width="${w$5 - 1.5}" height="${h - back - 1.75}" rx="3" fill="${FILL}" stroke="${DARK}" stroke-width="1" />
    <rect x="0.75" y="0.75" width="${w$5 - 1.5}" height="${back}" rx="2" fill="${DARK}" opacity="0.6" />
  `;
}
function plantShape(w$6, h) {
	const m = Math.min(w$6, h);
	const cx = w$6 / 2;
	const cy = h / 2;
	return w`
    <circle cx="${cx - m * .16}" cy="${cy - m * .1}" r="${m * .26}" fill="${FILL}" stroke="${DARK}" stroke-width="1" />
    <circle cx="${cx + m * .18}" cy="${cy - m * .06}" r="${m * .22}" fill="${FILL}" stroke="${DARK}" stroke-width="1" />
    <circle cx="${cx}" cy="${cy + m * .14}" r="${m * .24}" fill="${FILL}" stroke="${DARK}" stroke-width="1" />
    <circle cx="${cx}" cy="${cy}" r="${m * .12}" fill="${DARK}" opacity="0.65" />
  `;
}
function tvShape(w$7, h) {
	return w`
    <rect x="0.5" y="0.5" width="${w$7 - 1}" height="${h - 1}" rx="1.5" fill="${DARK}" />
    <rect x="1.8" y="1.8" width="${w$7 - 3.6}" height="${h - 3.6}" rx="1" fill="none" stroke="${LIGHT}" stroke-width="0.8" opacity="0.7" />
  `;
}
function deskShape(w$8, h) {
	return w`
    <rect x="0.75" y="0.75" width="${w$8 - 1.5}" height="${h - 1.5}" rx="3" fill="${FILL}" stroke="${DARK}" stroke-width="1" />
    <rect x="${w$8 * .2}" y="${h * .3}" width="${w$8 * .36}" height="${h * .4}" rx="2" fill="none" stroke="${DARK}" stroke-width="0.8" opacity="0.6" />
  `;
}
function counterShape(w$9, h) {
	const m = Math.min(w$9, h);
	const vertical = h >= w$9;
	const sink = vertical ? {
		cx: w$9 / 2,
		cy: h * .16
	} : {
		cx: w$9 * .16,
		cy: h / 2
	};
	const burners = vertical ? [
		{
			fx: .3,
			fy: .58
		},
		{
			fx: .7,
			fy: .58
		},
		{
			fx: .3,
			fy: .78
		},
		{
			fx: .7,
			fy: .78
		}
	] : [
		{
			fx: .58,
			fy: .3
		},
		{
			fx: .58,
			fy: .7
		},
		{
			fx: .78,
			fy: .3
		},
		{
			fx: .78,
			fy: .7
		}
	];
	return w`
    <rect x="0.75" y="0.75" width="${w$9 - 1.5}" height="${h - 1.5}" rx="3" fill="${FILL}" stroke="${DARK}" stroke-width="1" />
    <circle cx="${sink.cx}" cy="${sink.cy}" r="${m * .24}" fill="none" stroke="${DARK}" stroke-width="1" opacity="0.7" />
    ${burners.map(({ fx, fy }) => w`<circle cx="${w$9 * fx}" cy="${h * fy}" r="${m * .14}" fill="none" stroke="${DARK}" stroke-width="1" opacity="0.7" />`)}
  `;
}
function stairsShape(w$10, h) {
	const vertical = h >= w$10;
	const steps = 5;
	const lines = [];
	for (let i = 1; i <= steps; i++) {
		const t = i / 6;
		lines.push(vertical ? w`<line x1="2.5" y1="${h * t}" x2="${w$10 - 2.5}" y2="${h * t}" />` : w`<line x1="${w$10 * t}" y1="2.5" x2="${w$10 * t}" y2="${h - 2.5}" />`);
	}
	const arrow = vertical ? w`
      <line x1="${w$10 / 2}" y1="${h * .82}" x2="${w$10 / 2}" y2="${h * .2}" />
      <line x1="${w$10 / 2 - 3.5}" y1="${h * .2 + 4}" x2="${w$10 / 2}" y2="${h * .2}" />
      <line x1="${w$10 / 2 + 3.5}" y1="${h * .2 + 4}" x2="${w$10 / 2}" y2="${h * .2}" />
    ` : w`
      <line x1="${w$10 * .82}" y1="${h / 2}" x2="${w$10 * .2}" y2="${h / 2}" />
      <line x1="${w$10 * .2 + 4}" y1="${h / 2 - 3.5}" x2="${w$10 * .2}" y2="${h / 2}" />
      <line x1="${w$10 * .2 + 4}" y1="${h / 2 + 3.5}" x2="${w$10 * .2}" y2="${h / 2}" />
    `;
	return w`
    <rect x="0.75" y="0.75" width="${w$10 - 1.5}" height="${h - 1.5}" rx="2" fill="${FILL}" stroke="${DARK}" stroke-width="1" />
    <g stroke="${DARK}" stroke-width="0.9" opacity="0.65">${lines}</g>
    <g stroke="${DARK}" stroke-width="1" opacity="0.8">${arrow}</g>
  `;
}
var cellsToPx = cellsToPx$1;
var floorGridSize = floorGridSize$1;
var EPS = GRID_EPS;
var CHIP_PLACEHOLDER = {
	temperature: "—°",
	humidity: "—%",
	co2: "— ppm"
};
/** Responsive arrangement: side-by-side on wide containers, stacked below. */
function resolveLayoutDirection(containerWidth) {
	return containerWidth >= 1100 ? "row" : "column";
}
/**
* All room edges as merged wall segments: collinear edges on the same level
* (including edges different rooms share) collapse into single segments.
*/
function mergeWallSegments(rooms) {
	const byWall = /* @__PURE__ */ new Map();
	const add = (orient, level, from, to) => {
		const key = `${orient}:${Math.round(level / GRID_EPS)}`;
		const list = byWall.get(key);
		if (list === void 0) byWall.set(key, [{
			orient,
			level,
			from,
			to
		}]);
		else list.push({
			orient,
			level,
			from,
			to
		});
	};
	for (const room of rooms) {
		add("h", room.y, room.x, room.x + room.w);
		add("h", room.y + room.h, room.x, room.x + room.w);
		add("v", room.x, room.y, room.y + room.h);
		add("v", room.x + room.w, room.y, room.y + room.h);
	}
	const merged = [];
	for (const list of byWall.values()) {
		list.sort((a, b) => a.from - b.from);
		let current = list[0];
		if (current === void 0) continue;
		for (const next of list.slice(1)) if (next.from <= current.to + EPS) current = {
			...current,
			to: Math.max(current.to, next.to)
		};
		else {
			merged.push(current);
			current = next;
		}
		merged.push(current);
	}
	return merged;
}
/** Absolute grid interval a room's door cuts out of a wall. */
function doorGapFor(room, door) {
	const size = door.size;
	const at = door.at;
	switch (door.wall) {
		case "top": return {
			orient: "h",
			level: room.y,
			from: room.x + at,
			to: room.x + at + size,
			wall: "top"
		};
		case "bottom": return {
			orient: "h",
			level: room.y + room.h,
			from: room.x + at,
			to: room.x + at + size,
			wall: "bottom"
		};
		case "left": return {
			orient: "v",
			level: room.x,
			from: room.y + at,
			to: room.y + at + size,
			wall: "left"
		};
		case "right": return {
			orient: "v",
			level: room.x + room.w,
			from: room.y + at,
			to: room.y + at + size,
			wall: "right"
		};
	}
}
function doorGaps(floor) {
	const gaps = [];
	for (const room of floor.rooms) for (const door of room.doors ?? []) gaps.push(doorGapFor(room, door));
	return gaps;
}
/** Compact chip text from a raw HA state; null when not a usable number. */
function formatChipValue(kind, state) {
	const value = Number.parseFloat(state.replace(",", "."));
	if (!Number.isFinite(value)) return null;
	switch (kind) {
		case "temperature": return `${Math.round(value * 10) / 10}°`;
		case "humidity": return `${Math.round(value)}%`;
		case "co2": return `${Math.round(value)} ppm`;
	}
}
function renderFloorplan(config, hass, containerWidth, handlers) {
	const direction = resolveLayoutDirection(containerWidth);
	return b`
    <div class="body dock-${dockPlacement(containerWidth)}">
      <div class="floors" style="flex-direction:${direction}">
        ${config.floors.map((floor) => renderFloor(floor, config, hass, handlers))}
      </div>
      ${renderDock(config.dock ?? [], hass, handlers)}
    </div>
  `;
}
function renderFloor(floor, config, hass, handlers) {
	const cell = config.cell_px;
	const grid = floorGridSize(floor);
	const width = cellsToPx(grid.cols, cell);
	const height = cellsToPx(grid.rows, cell);
	const gaps = doorGaps(floor);
	return b`
    <section class="floor" style="width:${width}px">
      <div class="floor-name">${floor.name}</div>
      <div class="grid" data-floor=${floor.id} style="width:${width}px;height:${height}px">
        <svg
          class="surface"
          width="${width}"
          height="${height}"
          viewBox="0 0 ${width} ${height}"
          aria-hidden="true"
        >
          <path class="grid-line" d="${gridPath(grid, cell)}" />
          ${mergeWallSegments(floor.rooms).flatMap((segment) => wallLinesFor(segment, gaps, cell))}
          ${gaps.map((gap) => doorSwingSvg(gap, cell))}
        </svg>
        ${floor.rooms.map((room) => renderRoom(room, config, hass, cell, floor.id, handlers))}
        <floorhouse-glow
          class="glow"
          .floor=${floor}
          .config=${config}
          .hass=${hass}
        ></floorhouse-glow>
        ${(floor.objects ?? []).filter((object) => !isLightObject(object)).map((object) => renderObject(object, cell))}
        ${(floor.objects ?? []).filter(isLightObject).map((light) => renderLightMarker(light, hass, cell))}
      </div>
    </section>
  `;
}
function renderDock(dock, hass, handlers) {
	return b`
    <section class="dock" data-dock>
      <div class="dock-name">Без комнаты</div>
      <div class="dock-tiles">
        ${dock.map((slot, index) => {
		const tile = renderSlotTile(slot, hass, {
			floorId: "dock",
			dockIndex: index
		}, handlers);
		return tile === A ? A : b`<div class="dock-slot">${tile}</div>`;
	})}
        ${dock.length === 0 ? b`<span class="dock-empty">пусто</span>` : A}
      </div>
    </section>
  `;
}
function gridPath(grid, cell) {
	const w = cellsToPx(grid.cols, cell);
	const h = cellsToPx(grid.rows, cell);
	let d = "";
	for (let i = 0; i <= grid.cols; i++) d += `M ${i * cell} 0 V ${h} `;
	for (let i = 0; i <= grid.rows; i++) d += `M 0 ${i * cell} H ${w} `;
	return d;
}
function wallLinesFor(segment, doors, cell) {
	const gaps = doors.filter((gap) => gap.orient === segment.orient && Math.abs(gap.level - segment.level) <= 1e-6 && gap.from < segment.to + EPS && gap.to > segment.from - EPS).sort((a, b) => a.from - b.from);
	const lines = [];
	const emit = (from, to) => {
		if (to - from <= EPS) return;
		if (segment.orient === "h") lines.push(w`<line class="wall" x1="${from * cell}" y1="${segment.level * cell}" x2="${to * cell}" y2="${segment.level * cell}" />`);
		else lines.push(w`<line class="wall" x1="${segment.level * cell}" y1="${from * cell}" x2="${segment.level * cell}" y2="${to * cell}" />`);
	};
	let cursor = segment.from;
	for (const gap of gaps) {
		emit(cursor, Math.min(gap.from, segment.to));
		cursor = Math.max(cursor, gap.to);
	}
	emit(cursor, segment.to);
	return lines;
}
/** Door leaf + quarter swing arc, opening into the door's own room. */
function doorSwingSvg(door, cell) {
	const size = (door.to - door.from) * cell;
	let leaf;
	let arc;
	if (door.wall === "top" || door.wall === "bottom") {
		const hx = door.from * cell;
		const hy = door.level * cell;
		const into = door.wall === "top" ? 1 : -1;
		const sweep = door.wall === "top" ? 0 : 1;
		leaf = {
			x1: hx,
			y1: hy,
			x2: hx,
			y2: hy + into * size
		};
		arc = `M ${hx} ${hy + into * size} A ${size} ${size} 0 0 ${sweep} ${hx + size} ${hy}`;
	} else {
		const lx = door.level * cell;
		const fy = door.from * cell;
		const into = door.wall === "left" ? 1 : -1;
		const sweep = door.wall === "left" ? 1 : 0;
		leaf = {
			x1: lx,
			y1: fy,
			x2: lx + into * size,
			y2: fy
		};
		arc = `M ${lx + into * size} ${fy} A ${size} ${size} 0 0 ${sweep} ${lx} ${fy + size}`;
	}
	return w`
    <line class="door-leaf" x1="${leaf.x1}" y1="${leaf.y1}" x2="${leaf.x2}" y2="${leaf.y2}" />
    <path class="door-arc" d="${arc}" />
  `;
}
function renderRoom(room, config, hass, cell, floorId, handlers) {
	const meters2 = room.w * room.h * config.meters_per_cell * config.meters_per_cell;
	return b`
    <div
      class="room"
      style="left:${cellsToPx(room.x, cell)}px;top:${cellsToPx(room.y, cell)}px;width:${cellsToPx(room.w, cell)}px;height:${cellsToPx(room.h, cell)}px"
    >
      ${renderChips(room, config, hass)}
      <div class="label">
        <span class="name">${room.name}</span>
        <span class="area">${meters2.toFixed(1)} м²</span>
      </div>
      ${(room.slots ?? []).map((slot, index) => {
		const tile = renderSlotTile(slot, hass, {
			floorId,
			roomId: room.id,
			roomIndex: index
		}, handlers);
		if (tile === A) return A;
		const size = slotSize(slot, cell);
		return b`<div
          class="slot"
          data-floor=${floorId}
          data-room=${room.id}
          data-index=${index}
          style="left:${cellsToPx(slot.x ?? 0, cell)}px;top:${cellsToPx(slot.y ?? 0, cell)}px;width:${size.w}px;height:${size.h}px"
        >
          ${tile}
        </div>`;
	})}
    </div>
  `;
}
/** HA device classes that may feed climate chips (spec §3.4); soil-moisture
* (device_class 'moisture'/'humidity-soil') never qualifies — the reviewer
* showed the old 'почв' id regex could never match an ASCII entity id. */
var CHIP_DEVICE_CLASSES = [
	"temperature",
	"humidity",
	"carbon_dioxide",
	"pressure"
];
/** One chip's display text; placeholder when unconfigured/excluded/unreadable. */
function chipDisplay(kind, entityId, hass, exclude) {
	const entity = entityId === void 0 ? void 0 : hass?.states[entityId];
	const deviceClass = entity?.attributes["device_class"];
	const qualifies = typeof deviceClass === "string" && CHIP_DEVICE_CLASSES.includes(deviceClass);
	const excluded = entityId !== void 0 && isClimateExcluded(entityId, exclude);
	const value = entity !== void 0 && qualifies && !excluded ? formatChipValue(kind, entity.state) : null;
	return {
		text: value ?? CHIP_PLACEHOLDER[kind],
		placeholder: value === null
	};
}
function renderChips(room, config, hass) {
	const sensors = room.sensors ?? {};
	return b`
    <div class="chips">
      ${CHIP_KINDS.map((kind) => {
		const display = chipDisplay(kind, sensors[kind], hass, config.climate_exclude);
		return b`
          <span class="chip${display.placeholder ? " placeholder" : ""}">${display.text}</span>
        `;
	})}
    </div>
  `;
}
function renderObject(object, cell) {
	const w = cellsToPx(object.w, cell);
	const h = cellsToPx(object.h, cell);
	const rotation = object.rot === void 0 ? "" : `;transform:rotate(${object.rot}deg)`;
	return b`
    <div
      class="obj"
      style="left:${cellsToPx(object.x, cell)}px;top:${cellsToPx(object.y, cell)}px;width:${w}px;height:${h}px${rotation}"
    >
      ${furnitureSvg(object.type, w, h)}
    </div>
  `;
}
function renderLightMarker(light, hass, cell) {
	const entity = hass?.states[light.entity_id];
	const on = entity?.state === "on";
	const color = on ? rgbCss(resolveLightColor(entity.attributes)) : "#4d4d4d";
	return b`
    <div
      class="obj light${on ? " on" : ""}"
      style="left:${cellsToPx(light.x, cell) - 5}px;top:${cellsToPx(light.y, cell) - 5}px;width:10px;height:10px;background:${color}"
    ></div>
  `;
}
/** Styles for the floorplan; FloorhouseCard concatenates them with its shell. */
var floorplanStyles = i$3`
  .floors {
    display: flex;
    flex-wrap: wrap;
    gap: 34px 48px;
  }
  .floor {
    position: relative;
  }
  .floor-name {
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: #9a9a9a;
    margin: 0 0 2px 2px;
  }
  .grid {
    position: relative;
    margin-top: 26px; /* chip band above the top wall of first-row rooms */
    background: #242424;
    border-radius: 8px;
  }
  .surface {
    position: absolute;
    inset: 0;
    display: block;
  }
  .grid-line {
    stroke: #2e2e2e;
    stroke-width: 1;
    fill: none;
  }
  .wall {
    stroke: #8b8271;
    stroke-width: 2;
  }
  .door-leaf {
    stroke: #8b8271;
    stroke-width: 1.2;
    opacity: 0.6;
  }
  .door-arc {
    stroke: #8b8271;
    stroke-width: 1.2;
    opacity: 0.45;
    fill: none;
  }
  .room {
    position: absolute;
    background: #2b2b2b;
  }
  .room .label {
    position: absolute;
    left: 6px;
    top: 4px;
    z-index: 9;
    display: flex;
    flex-direction: column;
    pointer-events: none;
  }
  .room .name {
    font-size: 11px;
    line-height: 14px;
    color: #d2d2d2;
    white-space: nowrap;
  }
  .room .area {
    font-size: 9px;
    line-height: 11px;
    color: #858585;
  }
  .chips {
    position: absolute;
    left: 0;
    bottom: calc(100% + 4px);
    display: flex;
    gap: 4px;
    z-index: 6;
  }
  .chip {
    background: #212121;
    border: 1px solid #3b3b3b;
    border-radius: 999px;
    padding: 0 7px;
    font-size: 10px;
    line-height: 16px;
    color: #d6d6d6;
    white-space: nowrap;
  }
  .chip.placeholder {
    color: #616161;
    border-color: #2f2f2f;
  }
  .obj {
    position: absolute;
    z-index: 2;
  }
  .obj svg {
    display: block;
    width: 100%;
    height: 100%;
  }
  .glow {
    position: absolute;
    inset: 0;
    z-index: 4;
    pointer-events: none;
  }
  .glow canvas {
    display: block;
    width: 100%;
    height: 100%;
  }
  .obj.light {
    z-index: 5;
    border-radius: 50%;
    pointer-events: none;
  }
  .obj.light.on {
    box-shadow: 0 0 10px 3px rgba(255, 215, 160, 0.5);
  }

  /* dock + compact tiles (spec §3.1/§3.5) */
  .body.dock-below {
    display: block;
  }
  .body.dock-leading {
    display: flex;
    flex-direction: row;
    flex-wrap: wrap; /* floors wrap below the dock when the column leaves no room */
    align-items: flex-start;
    gap: 24px;
  }
  .dock {
    flex: none;
    max-width: 100%;
  }
  .dock-below .dock {
    margin-top: 22px;
  }
  .dock-leading .dock {
    order: -1; /* leading (left) edge: the dock column precedes the floors */
  }
  .dock-name {
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: #9a9a9a;
    margin: 0 0 6px 2px;
  }
  .dock-tiles {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    min-height: 56px;
  }
  .dock-leading .dock-tiles {
    flex-direction: column;
    align-items: stretch;
  }
  .dock-empty {
    font-size: 11px;
    color: #616161;
  }
  .slot {
    position: absolute;
    z-index: 7;
  }
  .tile {
    width: 100%;
    height: 100%;
    box-sizing: border-box;
    background: rgba(22, 28, 36, 0.88);
    border: 1px solid #3a4250;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
    padding: 2px 4px;
    font-size: 10px;
    color: #dbe3ec;
    cursor: grab;
    user-select: none;
    overflow: hidden;
    text-decoration: none;
  }
  .tile.on {
    border-color: #5a6b80;
  }
  .tile .ico {
    width: 16px;
    height: 16px;
    flex: none;
  }
  .tile .tap {
    background: none;
    border: 0;
    color: inherit;
    padding: 2px;
    cursor: pointer;
    font-size: 11px;
    line-height: 1;
    flex: none;
  }
  .tile .tap.expand {
    margin-left: auto;
  }
  .tile .tap.wide {
    border: 1px solid #3a4250;
    border-radius: 6px;
    padding: 4px 10px;
    width: 100%;
  }
  .tile .media-title {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex: 1;
    min-width: 0;
  }
  .tile .vol {
    width: 46px;
    flex: none;
    accent-color: #6fb3e8;
  }
  .tile .val {
    font-size: 12px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .tile .val small {
    font-size: 8px;
    margin-left: 1px;
    color: #93a0ac;
  }
  .tile .val.dim {
    color: #93a0ac;
    font-size: 10px;
  }
  .tile .spark {
    position: absolute;
    left: 3px;
    right: 3px;
    bottom: 2px;
    width: calc(100% - 6px);
    height: 12px;
  }
  .tile .spark path {
    fill: none;
    stroke: #6fb3e8;
    stroke-width: 1.2;
    opacity: 0.8;
  }
  .tile .badge {
    font-size: 9px;
    background: rgba(255, 255, 255, 0.08);
    border-radius: 999px;
    padding: 0 4px;
  }
  .tile.kind-plant.level-dry {
    color: #e08a6d;
  }
  .tile.kind-plant.level-ok {
    color: #8fce91;
  }
  .tile.kind-plant.level-wet {
    color: #6fb3e8;
  }
  .tile .ring {
    width: 26px;
    height: 26px;
    flex: none;
  }
  .tile .ring-text {
    fill: #dbe3ec;
    font-size: 9px;
  }
  .tile .p-meta {
    display: flex;
    flex-direction: column;
    gap: 1px;
    font-size: 9px;
    color: #93a0ac;
    white-space: nowrap;
  }
  .tile .sw-dot,
  .tile .net-dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: #57606b;
    flex: none;
  }
  .tile.on .sw-dot,
  .tile.on .net-dot {
    background: #8fce91;
    box-shadow: 0 0 6px 1px rgba(143, 206, 145, 0.6);
  }
  .tile .sw-label,
  .tile .net-label {
    font-size: 9px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .tile.kind-vacuum {
    justify-content: space-between;
  }
  .tile .vac-state {
    font-size: 10px;
    white-space: nowrap;
  }
  .tile .vac-actions {
    display: flex;
    gap: 2px;
  }
  .tile .embed-mark {
    font-family: ui-monospace, monospace;
    font-size: 12px;
    color: #9fb6cd;
  }
  .drag-ghost {
    position: fixed;
    z-index: 100;
    pointer-events: none;
    opacity: 0.85;
    background: rgba(22, 28, 36, 0.95);
    border: 1px solid #5a6b80;
    border-radius: 8px;
    padding: 4px 8px;
    font-size: 11px;
    color: #dbe3ec;
  }
  .popover-backdrop {
    position: fixed;
    inset: 0;
    z-index: 90;
    background: rgba(5, 8, 14, 0.6);
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .embed-popover {
    background: #1a222c;
    border: 1px solid #3a4250;
    border-radius: 12px;
    padding: 16px;
    min-width: 280px;
    max-width: min(90vw, 560px);
    color: #dbe3ec;
    font-size: 12px;
  }
  .embed-popover .embed-fallback pre {
    font-size: 11px;
    color: #9fb6cd;
    overflow: auto;
    max-height: 50vh;
  }
  .aisis {
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-width: 300px;
  }
  .aisis-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .aisis-title {
    margin-left: auto;
    color: #93a0ac;
  }
  .conn {
    width: 10px;
    height: 10px;
    border-radius: 50%;
  }
  .conn.on {
    background: #8fce91;
  }
  .conn.off {
    background: #57606b;
  }
  .aisis .vol {
    flex: 1;
    accent-color: #6fb3e8;
  }
  .gauge {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .gauge-bar {
    flex: 1;
    height: 6px;
    background: #2a3340;
    border-radius: 999px;
    overflow: hidden;
  }
  .gauge-fill {
    height: 100%;
    background: linear-gradient(90deg, #8fce91, #e0c46d, #e08a6d);
  }
  .tts {
    background: #121820;
    color: #dbe3ec;
    border: 1px solid #3a4250;
    border-radius: 8px;
    padding: 6px 8px;
    font: inherit;
    resize: vertical;
  }
`;
// src/floorhouse-card.ts
/**
* @file `custom:floorhouse-card` — single-page floorplan home dashboard card.
*
* Accessed via: HA Lovelace loads dist/floorhouse-card.js (registered in the
* HA config's lovelace resources as type: module), instantiates the element
* and calls setConfig, assigns `hass`, and reads getCardSize. The demo harness
* (demo/index.html) mounts it against demo/mock-hass.ts without a live HA.
*
* Assumptions: this file is the shell — config parsing boundary, hass
* assignment, container-width observation, the tile drag-and-drop glue
* (pointer → GridBox → pure applySlotDrop) and the embed/AISIS popover.
* The floorplan renders via src/render/floorplan.ts; tile drag positions are
* computed from live grid rects so the math stays in DOM-free modules.
*/
var DRAG_THRESHOLD = 4;
var FloorhouseCard = class extends i {
	constructor(..._args) {
		super(..._args);
		this._width = 0;
		this._popover = void 0;
		this._drag = void 0;
		this._tileHandlers = {
			onPointerDown: (event, source, slot) => {
				this._onTilePointerDown(event, source, slot);
			},
			onOpen: (slot) => {
				this._popover = slot;
			}
		};
	}
	setConfig(config) {
		this._config = parseFloorhouseConfig(config);
	}
	getCardSize() {
		return 6;
	}
	connectedCallback() {
		super.connectedCallback();
		if (typeof ResizeObserver === "undefined") return;
		this._resizeObserver ??= new ResizeObserver((entries) => {
			const raw = entries[0]?.contentRect.width ?? 0;
			const width = Math.round(raw / 8) * 8;
			if (width > 0 && width !== this._width) this._width = width;
		});
		this._resizeObserver.observe(this);
	}
	disconnectedCallback() {
		this._drag?.ghost.remove();
		this._drag = void 0;
		this._resizeObserver?.disconnect();
		super.disconnectedCallback();
	}
	_onTilePointerDown(event, source, slot) {
		if (this._drag !== void 0 || event.button !== 0) return;
		const target = event.target;
		if (target !== null && target.closest("button, input, textarea, a") !== null) return;
		event.preventDefault();
		const ghost = document.createElement("div");
		ghost.className = "drag-ghost";
		ghost.textContent = slot.title ?? slot.entity ?? slot.kind;
		const drag = {
			origin: {
				floorId: source.floorId,
				...source.roomId === void 0 ? {} : { roomId: source.roomId },
				...source.roomIndex === void 0 ? {} : { roomIndex: source.roomIndex },
				...source.dockIndex === void 0 ? {} : { dockIndex: source.dockIndex }
			},
			slot,
			ghost,
			startX: event.clientX,
			startY: event.clientY,
			moved: false,
			onMove: () => {},
			onUp: () => {}
		};
		drag.onMove = (move) => {
			if (!drag.moved && Math.hypot(move.clientX - drag.startX, move.clientY - drag.startY) < DRAG_THRESHOLD) return;
			if (!drag.moved) {
				drag.moved = true;
				this.renderRoot.appendChild(ghost);
			}
			ghost.style.left = `${move.clientX + 8}px`;
			ghost.style.top = `${move.clientY + 8}px`;
		};
		drag.onUp = (up) => {
			document.removeEventListener("pointermove", drag.onMove);
			document.removeEventListener("pointerup", drag.onUp);
			ghost.remove();
			this._drag = void 0;
			if (!drag.moved) {
				if (slot.kind === "embed" || slot.kind === "media_player" && (slot.entity ?? "").includes("aisis")) this._popover = slot;
				return;
			}
			const target = dropTargetAt(this._gridBoxes(), {
				x: up.clientX,
				y: up.clientY
			});
			if (target === null || this._config === void 0) return;
			const next = applySlotDrop(this._config, drag.origin, target);
			if (next !== this._config) this._config = next;
		};
		this._drag = drag;
		document.addEventListener("pointermove", drag.onMove);
		document.addEventListener("pointerup", drag.onUp);
	}
	/** Live floor-grid rects for drop hit-testing (spec §3.1/§3.5). */
	_gridBoxes() {
		const config = this._config;
		if (config === void 0) return [];
		const cell = resolveCellPx(config.cell_px, Math.max(0, this._width - 34), Math.max(...config.floors.map((floor) => floorGridSize$1(floor).cols)));
		const boxes = [];
		for (const floor of config.floors) {
			const grid = this.renderRoot.querySelector(`.grid[data-floor="${floor.id}"]`);
			if (grid === null) continue;
			const rect = grid.getBoundingClientRect();
			boxes.push({
				floorId: floor.id,
				left: rect.left,
				top: rect.top,
				width: rect.width,
				height: rect.height,
				cell,
				rooms: floor.rooms
			});
		}
		return boxes;
	}
	_closePopover() {
		this._popover = void 0;
	}
	render() {
		const config = this._config;
		if (!config) return A;
		const maxCols = Math.max(...config.floors.map((floor) => floorGridSize$1(floor).cols));
		const effCell = resolveCellPx(config.cell_px, Math.max(0, this._width - 34), maxCols);
		const view = effCell === config.cell_px ? config : {
			...config,
			cell_px: effCell
		};
		return b`
      <div class="card" style="${skyVars(this.hass)}">
        ${renderSky(this.hass)} ${renderHeaderBand(this.hass)}
        ${config.title === void 0 ? A : b`<h1 class="title">${config.title}</h1>`}
        ${renderFloorplan(view, this.hass, this._width, this._tileHandlers)}
        ${this._renderPopover()}
      </div>
    `;
	}
	_renderPopover() {
		const slot = this._popover;
		if (slot === void 0) return A;
		const embed = b`<floorhouse-embed .card=${slot.card} .hass=${this.hass}></floorhouse-embed>`;
		return b`
      <div class="popover-backdrop" @click=${() => this._closePopover()}>
        <div
          class="embed-popover"
          role="dialog"
          @click=${(event) => event.stopPropagation()}
        >
          ${slot.kind === "embed" || !(slot.entity ?? "").includes("aisis") ? embed : renderAisisExpanded(slot, this.hass)}
        </div>
      </div>
    `;
	}
	static {
		this.styles = [
			i$3`
      :host {
        display: block;
      }
      .card {
        background: linear-gradient(175deg, var(--fh-bg-a, #1c1c1c), var(--fh-bg-b, #161616));
        color: #e1e1e1;
        border: 1px solid #2c2c2c;
        border-radius: 12px;
        padding: 16px;
        font-family: system-ui, sans-serif;
      }
      .title {
        font-size: 20px;
        font-weight: 600;
        margin: 0 0 12px;
      }
    `,
			floorplanStyles,
			skyStyles,
			headerStyles
		];
	}
};
__decorate([n({ attribute: false })], FloorhouseCard.prototype, "hass", void 0);
__decorate([r()], FloorhouseCard.prototype, "_config", void 0);
__decorate([r()], FloorhouseCard.prototype, "_width", void 0);
__decorate([r()], FloorhouseCard.prototype, "_popover", void 0);
if (typeof customElements !== "undefined" && !customElements.get("floorhouse-card")) customElements.define("floorhouse-card", FloorhouseCard);
if (typeof window !== "undefined") {
	const cards = window["customCards"] ?? (window["customCards"] = []);
	if (!cards.some((card) => card.type === "floorhouse-card")) cards.push({
		type: "floorhouse-card",
		name: "Floorhouse Card",
		description: "Single-page floorplan home dashboard"
	});
}
export { FloorhouseCard };
