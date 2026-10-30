// Runs the bridge pilot page's real script against a stubbed DOM and store, and
// prints what each scenario ends with. tests/test_bridge_pilot.py checks the output.
import { readFileSync } from "node:fs";
import vm from "node:vm";

const page = readFileSync(process.argv[2], "utf8");
const pairsText = page.match(/type="application\/json">([\s\S]*?)<\/script>/)[1];
const script = page.slice(page.lastIndexOf("<script>") + 8, page.lastIndexOf("</script>"));

function element(id) {
  return {
    id, hidden: id === "finished", textContent: id === "pairs" ? pairsText : "", value: "",
    href: "", disabled: false, dataset: {}, listeners: {},
    classList: { toggle() {} },
    setAttribute(name, value) { this[name] = value; },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    append(child) { (this.children ??= []).push(child); },
    select() {},
    click() { return this.listeners.click?.(); },
  };
}

// One device: the store and local storage outlive a reload; the elements don't.
function load(device, store) {
  const elements = {};
  const get = (id) => (elements[id] ??= element(id));
  const choices = ["A", "B", "unplayable"].map((pick) => {
    const button = element(`pick-${pick}`);
    button.dataset.pick = pick;
    return button;
  });
  const document = {
    getElementById: get,
    createElement: () => element("cell"),
    querySelectorAll: (selector) => (selector === ".choice" ? choices : get("sheet").children ?? []),
  };
  const localStorage = {
    getItem: (key) => device.storage[key] ?? null,
    setItem: (key, value) => { device.storage[key] = value; },
  };
  const window = { scrollTo() {}, claude: { use: async () => store.api } };
  vm.runInNewContext(script, {
    document, window, localStorage, navigator: {}, console,
    Date: { now: () => ++store.clock },
    JSON, Object, Set, Boolean, Number, String, Promise, encodeURIComponent,
  });
  const pick = (value) => choices.find((c) => c.dataset.pick === value).click();
  return { elements, get, pick, picks: () => JSON.parse(get("picks-text").value) };
}

function newStore() {
  const store = {
    docs: {}, collections: new Set(), failing: false, clock: 1000, hold: null,
    api: {
      collection(name) {
        store.collections.add(name);
        return {
          get: async () => ({
            docs: Object.keys(store.docs).filter((k) => k.startsWith(`${name}/`))
              .map((k) => ({ data: () => store.docs[k] })),
          }),
          doc: (id) => ({
            get: async () => {
              if (store.failing) throw new Error("down");
              const row = store.docs[`${name}/${id}`];
              return { exists: Boolean(row), data: () => row };
            },
            set: async (row) => {
              if (store.failing) throw new Error("down");
              if (store.hold) await store.hold;
              store.docs[`${name}/${id}`] = row;
            },
          }),
        };
      },
    },
  };
  return store;
}

const settle = async () => { for (let i = 0; i < 20; i += 1) await new Promise(setImmediate); };
const device = () => ({ storage: {} });
const firstPick = (store) => Object.values(store.docs).find((row) => row.pair === 1)?.pick;

const scenarios = {
  async failed_write_survives_reload() {
    const store = newStore(), phone = device();
    let view = load(phone, store);
    await settle();
    await view.pick("A");
    await settle();
    store.failing = true;
    await view.pick("B");
    await settle();
    const warned = view.get("warning").hidden === false;
    store.failing = false;
    view = load(phone, store);
    await settle();
    return { warned, local: view.picks()["1"], stored: firstPick(store) };
  },
  async warning_stays_until_every_failed_write_lands() {
    const store = newStore(), phone = device();
    const view = load(phone, store);
    await settle();
    await view.pick("A");
    await settle();
    store.failing = true;
    await view.pick("B");
    await settle();
    // Only the failed pair is now outstanding; a read failure keeps it failing.
    const whileDown = view.get("warning").hidden === false;
    store.failing = false;
    await view.get("next").click();
    await view.pick("A");
    await settle();
    return { whileDown, afterRetry: view.get("warning").hidden === false, stored: firstPick(store) };
  },
  async newer_pick_from_another_device_wins() {
    const store = newStore(), phone = device(), laptop = device();
    const onPhone = load(phone, store);
    await settle();
    await onPhone.pick("A");
    await settle();
    const onLaptop = load(laptop, store);
    await settle();
    onLaptop.get("sheet").children[0].click();
    await onLaptop.pick("B");
    await settle();
    const again = load(phone, store);
    await settle();
    return { phone: again.picks()["1"], stored: firstPick(store) };
  },
  async older_write_does_not_overwrite_a_newer_stored_pick() {
    const store = newStore(), phone = device();
    const view = load(phone, store);
    await settle();
    await view.pick("A");
    // Another device saved B after this pick was made, before this write ran.
    store.failing = true;
    await view.pick("A");
    await settle();
    store.failing = false;
    const kitDocs = [...store.collections][0];
    store.docs[`${kitDocs}/pair-1`] = { pair: 1, pick: "B", at: store.clock + 500 };
    const again = load(phone, store);
    await settle();
    return { local: again.picks()["1"], stored: firstPick(store) };
  },
  async last_of_two_quick_picks_lands() {
    const store = newStore(), phone = device();
    const view = load(phone, store);
    await settle();
    let release;
    store.hold = new Promise((resolve) => { release = resolve; });
    view.pick("A");
    await settle();
    view.pick("B");
    await settle();
    store.hold = null;
    release();
    await settle();
    return { stored: firstPick(store) };
  },
  async picks_live_in_a_collection_of_this_kit() {
    const store = newStore();
    const view = load(device(), store);
    await settle();
    await view.pick("A");
    await settle();
    return { collections: [...store.collections] };
  },
};

const results = {};
for (const [name, run] of Object.entries(scenarios)) results[name] = await run();
console.log(JSON.stringify(results));
