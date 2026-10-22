// iCUE widget runtime shim for Linux. Injected at DocumentCreation, before
// any widget script, with __ICUE_CFG__ replaced by the host.
(function () {
  "use strict";
  const cfg = __ICUE_CFG__;
  const w = window;

  // ---- iCUE global object + device + per-instance id ----------------------
  w.iCUE = Object.assign({}, cfg.iCUE, {
    defaultTemperatureUnit: function () { return cfg.temperatureUnit; },
  });
  w.device = Object.freeze({ deviceId: cfg.deviceId });
  w.uniqueId = cfg.uniqueId;
  w.iCUE_initialized = false;
  if (typeof w.icueEvents === "undefined") w.icueEvents = {};

  // ---- property globals (x-icue-property) ---------------------------------
  for (const [k, v] of Object.entries(cfg.properties)) w[k] = v;

  // ---- tr(): Promise-returning at runtime, per the spec -------------------
  function translate(key) {
    for (const code of [cfg.lang, cfg.lang.split("_")[0], "en"]) {
      const t = (cfg.translations[code] || {}).translation || {};
      if (Object.prototype.hasOwnProperty.call(t, key)) return t[key];
    }
    return key;
  }
  w.tr = function (key) { return Promise.resolve(translate(String(key))); };

  // ---- plugin stubs that exist synchronously, bound to QWebChannel later --
  w.plugins = w.plugins || {};
  const stubs = {};
  for (const p of cfg.plugins) {
    const d = p.describe;
    const stub = { __queue: [], __real: null };
    for (const sig of d.signals) {
      const pending = [];
      stub[sig] = {
        connect(fn) { stub.__real ? stub.__real[sig].connect(fn) : pending.push(["c", fn]); },
        disconnect(fn) { stub.__real ? stub.__real[sig].disconnect(fn) : pending.push(["d", fn]); },
        __pending: pending,
      };
    }
    for (const m of d.methods) {
      if (d.blocking[m]) {
        const fn = new Function("ctx", "args", d.blocking[m]);
        stub[m] = function (...args) { return fn(p.ctx, args); };
      } else {
        stub[m] = function (...args) {
          if (stub.__real) return stub.__real[m](...args);
          stub.__queue.push([m, args]);
        };
      }
    }
    for (const prop of d.properties) {
      Object.defineProperty(stub, prop, {
        get() { return stub.__real ? stub.__real[prop] : undefined; },
        enumerable: true,
      });
    }
    stubs[p.key] = stub;
    w.plugins[p.key] = stub;
    w["plugin" + p.key + "_initialized"] = false;
  }

  function fire(obj, name, ...args) {
    try {
      if (obj && typeof obj[name] === "function") obj[name](...args);
    } catch (e) { console.error("[icue-linux] " + name + " handler threw", e); }
  }

  // ---- host -> page API (property updates) --------------------------------
  w.__icueHost = {
    setProperties(values) {
      for (const [k, v] of Object.entries(values)) w[k] = v;
      if (w.iCUE_initialized) fire(w.icueEvents, "onDataUpdated");
    },
  };

  // ---- bring-up: channel ready + DOM parsed -> initialise -----------------
  let channelReady = cfg.plugins.length === 0;
  let domReady = document.readyState !== "loading";

  function maybeInit() {
    if (!channelReady || !domReady || w.iCUE_initialized) return;
    for (const p of cfg.plugins) {
      w["plugin" + p.key + "_initialized"] = true;
      fire(w["plugin" + p.key + "Events"], "onInitialized");
    }
    w.iCUE_initialized = true;
    fire(w.icueEvents, "onICUEInitialized");
    fire(w.icueEvents, "onDataUpdated");
  }

  document.addEventListener("DOMContentLoaded", () => { domReady = true; maybeInit(); });

  if (cfg.plugins.length) {
    const start = () => new QWebChannel(qt.webChannelTransport, (channel) => {
      for (const p of cfg.plugins) {
        const real = channel.objects[p.key];
        const stub = stubs[p.key];
        if (!real) { console.warn("[icue-linux] plugin missing on channel: " + p.key); continue; }
        stub.__real = real;
        for (const sig of p.describe.signals) {
          for (const [op, fn] of stub[sig].__pending) real[sig][op === "c" ? "connect" : "disconnect"](fn);
          stub[sig].__pending.length = 0;
        }
        for (const [m, args] of stub.__queue) real[m](...args);
        stub.__queue.length = 0;
      }
      channelReady = true;
      maybeInit();
    });
    if (typeof qt !== "undefined" && qt.webChannelTransport) start();
    else document.addEventListener("DOMContentLoaded", start);
  }
})();
