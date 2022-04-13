// DO NOT EDIT! This test has been generated by /html/canvas/tools/gentest.py.
// OffscreenCanvas test in a worker:2d.clearRect.nonfinite
// Description:clearRect() with Infinity/NaN is ignored
// Note:

importScripts("/resources/testharness.js");
importScripts("/html/canvas/resources/canvas-tests.js");

var t = async_test("clearRect() with Infinity/NaN is ignored");
var t_pass = t.done.bind(t);
var t_fail = t.step_func(function(reason) {
    throw reason;
});
t.step(function() {

var canvas = new OffscreenCanvas(100, 50);
var ctx = canvas.getContext('2d');

ctx.fillStyle = '#0f0';
ctx.fillRect(0, 0, 100, 50);
ctx.clearRect(Infinity, 0, 100, 50);
ctx.clearRect(-Infinity, 0, 100, 50);
ctx.clearRect(NaN, 0, 100, 50);
ctx.clearRect(0, Infinity, 100, 50);
ctx.clearRect(0, -Infinity, 100, 50);
ctx.clearRect(0, NaN, 100, 50);
ctx.clearRect(0, 0, Infinity, 50);
ctx.clearRect(0, 0, -Infinity, 50);
ctx.clearRect(0, 0, NaN, 50);
ctx.clearRect(0, 0, 100, Infinity);
ctx.clearRect(0, 0, 100, -Infinity);
ctx.clearRect(0, 0, 100, NaN);
ctx.clearRect(Infinity, Infinity, 100, 50);
ctx.clearRect(Infinity, Infinity, Infinity, 50);
ctx.clearRect(Infinity, Infinity, Infinity, Infinity);
ctx.clearRect(Infinity, Infinity, 100, Infinity);
ctx.clearRect(Infinity, 0, Infinity, 50);
ctx.clearRect(Infinity, 0, Infinity, Infinity);
ctx.clearRect(Infinity, 0, 100, Infinity);
ctx.clearRect(0, Infinity, Infinity, 50);
ctx.clearRect(0, Infinity, Infinity, Infinity);
ctx.clearRect(0, Infinity, 100, Infinity);
ctx.clearRect(0, 0, Infinity, Infinity);
_assertPixel(canvas, 50,25, 0,255,0,255, "50,25", "0,255,0,255");

t.done();

});
done();
