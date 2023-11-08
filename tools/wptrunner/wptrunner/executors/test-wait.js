var callback = arguments[arguments.length - 1];
var observer = null;
var root = document.documentElement;

var error_handler = function(error, message, stack) {
  callback("error");
};

addEventListener("error", function(e) {
    var message = e.message;
    var stack;
    if (e.error && e.error.stack) {
        stack = e.error.stack;
    } else {
        stack = e.filename + ":" + e.lineno + ":" + e.colno;
    }
    error_handler(e.error, message, stack);
}, false);

addEventListener("unhandledrejection", function(e) {
    var message;
    if (e.reason && e.reason.message) {
        message = "Unhandled rejection: " + e.reason.message;
    } else {
        message = "Unhandled rejection";
    }
    var stack;
    if (e.reason && e.reason.stack) {
        stack = e.reason.stack;
    }
    error_handler(e.reason, message, stack);
}, false);
 
function wait_load() {
  if (Document.prototype.hasOwnProperty("fonts")) {
    document.fonts.ready.then(wait_paints);
  } else {
    // This might take the screenshot too early, depending on whether the
    // load event is blocked on fonts being loaded. See:
    // https://github.com/w3c/csswg-drafts/issues/1088
    wait_paints();
  }
}


function wait_paints() {
  // As of 2017-04-05, the Chromium web browser exhibits a rendering bug
  // (https://bugs.chromium.org/p/chromium/issues/detail?id=708757) that
  // produces instability during screen capture. The following use of
  // `requestAnimationFrame` is intended as a short-term workaround, though
  // it is not guaranteed to resolve the issue.
  //
  // For further detail, see:
  // https://github.com/jugglinmike/chrome-screenshot-race/issues/1

  requestAnimationFrame(function() {
    requestAnimationFrame(function() {
      screenshot_if_ready();
    });
  });
}

function screenshot_if_ready() {
  if (root &&
      root.classList.contains("%(classname)s") &&
      observer === null) {
    observer = new MutationObserver(wait_paints);
    observer.observe(root, {attributes: true});
    var event = new Event("TestRendered", {bubbles: true});
    root.dispatchEvent(event);
    return;
  }
  if (observer !== null) {
    observer.disconnect();
  }
  callback("ready");
}

setTimeout(function() {
  removeEventListener('load', wait_load);
  if (observer !== null) {
    observer.disconnect();
  }
  callback("timeout");
}, %(timeout)f * 1000);

if (document.readyState != "complete") {
  addEventListener('load', wait_load);
} else {
  wait_load();
}
