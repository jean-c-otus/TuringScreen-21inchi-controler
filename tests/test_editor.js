// Run with: gjs tests/test_editor.js static/index.html
// Unit tests with DOM doubles; no browser, desktop instance or USB access.
const Gio = imports.gi.Gio;
const [, bytes] = Gio.File.new_for_path(ARGV[0] || "static/index.html").load_contents(null);
const html = new TextDecoder().decode(bytes);
const script = html.split("<script>")[1].split("</script>")[0];
new Function(script); // Parse the complete production script without running it.

function check(condition, message) {
    if (!condition) throw new Error(message);
}

function section(start, end) {
    return script.slice(script.indexOf(start), script.indexOf(end, script.indexOf(start)));
}

function fixture() {
    const nodes = new Map();
    const $ = id => {
        if (!nodes.has(id)) nodes.set(id, {
            value: "", style: {}, disabled: false, children: [],
            classList: {add() {}, remove() {}},
            replaceChildren() { this.children = []; },
            appendChild(child) { this.children.push(child); },
        });
        return nodes.get(id);
    };
    $("rotation").value = "30";
    $("fit").value = "contain";
    $("zoom").value = "1";
    $("background").value = "#101827";
    class FakeImage {
        constructor() { this.style = {}; this.naturalWidth = 100; this.naturalHeight = 50; }
    }
    class HTMLVideoElement {
        constructor() { this.style = {}; this.videoWidth = 100; this.videoHeight = 50; this.events = {}; this.paused = true; }
        pause() { this.paused = true; }
        play() { this.paused = false; return Promise.resolve(); }
        addEventListener(name, callback) { this.events[name] = callback; }
    }
    const Image = FakeImage;
    const document = {createElement: () => new HTMLVideoElement()};
    const URL = {createObjectURL: () => "blob:unit", revokeObjectURL() {}};
    const drawing = [];
    const ctx = {
        clearRect() { drawing.push("clear"); }, save() {}, restore() {},
        measureText() { return {width: 60}; },
        fillText() { drawing.push("overlay"); },
        drawImage() { throw new Error("Animated media must remain a native element"); },
        fillRect() { throw new Error("Overlay canvas must remain transparent"); },
    };
    let sourceUrl = null, sourceLoadId = 0, rawSource = null, sourceReady = false, sourceKind = "";
    let media = {id: "draft", revision: 1}, dirty = false, busy = false, renderRunning = false, uploadRunning = false;
    let lastJobState = "idle", exampleFound = false, selectedOverlay = -1, cropX = 0, cropY = 0;
    let overlays = [{text: "TOP", x: 20, y: 20, color: "#00ff00"}];
    let editGeneration = 1, autoSaveTimer = null, projects = [], calls = 0;
    const fontFamily = () => "sans-serif";
    const overlayText = row => row.text;
    const showFree = () => {};
    const setSaveLabel = () => {};
    const message = () => {};
    const clearTimeout = () => {};
    const scheduleAutoRender = () => {};
    const settings = () => ({rotation: Number($("rotation").value)});
    const deviceValues = () => ({});
    const api = async () => { calls++; return {project: {...media, revision: 2}, projects: []}; };
    const useMedia = project => { media = project; dirty = false; };
    const functions = [
        section("function refreshButtons()", "function setSaveLabel("),
        section("function drawEditor()", "async function loadWorkspace("),
        section("async function renderNow(", "async function upload("),
    ].join("\n");
    return eval(functions + `\n({loadLocalSource,drawEditor,refreshButtons,renderNow,$,drawing,
        state:()=>({sourceKind,sourceReady,rawSource,calls}),
        change:fields=>{ if('dirty' in fields)dirty=fields.dirty;
                        if('busy' in fields)busy=fields.busy;
                        if('lastJobState' in fields)lastJobState=fields.lastJobState;
                        if('uploadRunning' in fields)uploadRunning=fields.uploadRunning; }
    })`);
}

async function main() {
    const editor = fixture();
    editor.loadLocalSource({name: "renamed scheme", type: "image/gif"}, "video");
    check(editor.state().sourceKind === "gif", "Saved GIF must not become a video element");
    check(editor.$("sourceMedia").children.length === 1, "Native GIF must be attached below the overlay canvas");
    editor.state().rawSource.onload();
    editor.drawEditor();
    check(editor.drawing.join(",") === "clear,overlay", "Text must be drawn over a transparent animated background");
    check(Math.abs(parseFloat(editor.state().rawSource.style.width) - 100 / 112 * 100) < 1e-8,
          "Arbitrary angle preview must use expanded bounds, not quarter-turn dimensions");

    const previous = editor.state().rawSource;
    editor.loadLocalSource({name: "movie.mp4", type: "video/mp4"}, "video");
    const video = editor.state().rawSource;
    previous.onload();
    check(!editor.state().sourceReady, "An old image callback must not replace a new video");
    video.events.loadeddata();
    editor.drawEditor();
    check(video.loop && video.muted && !video.paused, "Editing video must keep playing natively");

    const buttons = fixture();
    buttons.refreshButtons();
    check(!buttons.$("render").disabled && !buttons.$("send").disabled,
          "A newly uploaded clean draft must allow manual rendering and sending");
    buttons.change({dirty: true});
    buttons.refreshButtons();
    check(!buttons.$("render").disabled && buttons.$("send").disabled,
          "An edited draft must allow rendering before transmission");
    buttons.change({dirty: false, uploadRunning: true});
    buttons.refreshButtons();
    check(buttons.$("render").disabled && buttons.$("send").disabled, "Upload in progress must serialize controls");
    buttons.change({uploadRunning:false,busy:true,lastJobState:"retrying"});
    buttons.refreshButtons();
    check(!buttons.$("reconnect").disabled,"Manual USB reset must remain available during retry waits");
    buttons.change({lastJobState:"checking"});
    buttons.refreshButtons();
    check(buttons.$("refresh").disabled,"A handshake check must not start competing USB operations");

    const native = fixture();
    native.$("transport").value="device";
    native.refreshButtons();
    check(native.$("bench").disabled,"MCU playback must never run a host-frame bandwidth test");
    native.change({busy:true,lastJobState:"live"});
    native.refreshButtons();
    check(!native.$("render").disabled,"Additional layers must remain editable during native playback");
    check(native.$("fps").disabled && native.$("rotation").disabled && native.$("transport").disabled,
          "Native backgrounds and transport must remain fixed until stopped");

    const rendering = fixture();
    await rendering.renderNow();
    check(rendering.state().calls === 0, "Clean drafts must not automatically rerender forever");
    await rendering.renderNow(true);
    check(rendering.state().calls === 1, "Manual render must work on a clean uploaded draft");
    print("PASS: editor script syntax, animated GIF restoration, native playback, overlay ordering, arbitrary-angle bounds, buttons and manual render");
}

main().catch(error => { printerr(error.stack); imports.system.exit(1); });
