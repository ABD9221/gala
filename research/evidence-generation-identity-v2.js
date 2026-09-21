// Private client state is namespaced so an older embed cannot overwrite it.
(() => {
  const ID_KEY = 'generation-v2-browser';
  const validId = value => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);
  const memory = new Map();
  const read = key => {
    if (memory.has(key)) return memory.get(key);
    try { return localStorage.getItem(key); } catch { return null; }
  };
  const write = (key, value) => {
    value = String(value);
    try { localStorage.setItem(key, value); memory.delete(key); }
    catch { memory.set(key, value); }
  };
  let id;
  const initialize = () => {
    let stored = read(ID_KEY);
    if (!validId(stored)) {
      stored = [...crypto.getRandomValues(new Uint8Array(16))].map(n => n.toString(16).padStart(2,'0')).join('');
      write(ID_KEY, stored);
    }
    id = stored;
    return id;
  };
  const ready = globalThis.navigator?.locks
    ? navigator.locks.request(ID_KEY, initialize).catch(initialize)
    : Promise.resolve(initialize());
  const storage = new Proxy({}, {
    get(_target, property) {
      if (typeof property !== 'string' || !id) return undefined;
      const value = read(`generation-v2:${id}:${property}`);
      if (property.startsWith('userKey-') && value && !/^[a-f0-9]{64}$/.test(value)) {
        write(`generation-v2:${id}:${property}`, '');
        return '';
      }
      if (property === 'removeItem') return key => write(`generation-v2:${id}:${key}`, '');
      return value === null ? undefined : value;
    },
    set(_target, property, value) {
      if (id) write(`generation-v2:${id}:${property}`, value);
      return true;
    },
    deleteProperty(_target, property) {
      if (id) write(`generation-v2:${id}:${property}`, '');
      return true;
    },
  });
  window.generationIdentity = {
    ready, storage,
    get id() { return id; },
    async ensure() { await ready; const stored = read(ID_KEY); if (validId(stored)) id = stored; else write(ID_KEY, id); return id; },
    setKey(thread, key, expectedId = id) {
      if (expectedId !== id || !/^[a-f0-9]{64}$/.test(key || '')) return false;
      storage[`userKey-${thread}`] = key;
      return true;
    },
  };
})();
