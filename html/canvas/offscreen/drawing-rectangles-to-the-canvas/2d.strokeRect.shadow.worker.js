// DO NOT EDIT! This test has been generated by /html/canvas/tools/gentest.py.
// OffscreenCanvas test in a worker:2d.strokeRect.shadow
// Description:strokeRect draws shadows
// Note:

importScripts("/resources/testharness.js");
importScripts("/html/canvas/resources/canvas-tests.js");

var t = async_test("strokeRect draws shadows");
var t_pass = t.done.bind(t);
var t_fail = t.step_func(function(reason) {
    throw reason;
});
t.step(function() {

var canvas = new OffscreenCanvas(100, 50);
var ctx = canvas.getContext('2d');

ctx.fillStyle = '#f00';
ctx.fillRect(0, 0, 100, 50);
ctx.fillStyle = '#f00';
ctx.shadowColor = '#0f0';
ctx.shadowBlur = 0;
ctx.shadowOffsetX = 0;
ctx.shadowOffsetY = 50;
ctx.strokeStyle = '#f00';
ctx.lineWidth = 50;
ctx.strokeRect(0, -75, 100, 50);
_assertPixel(canvas, 50,25, 0,255,0,255, "50,25", "0,255,0,255");

t.done();

});
done();
