// DO NOT EDIT! This test has been generated by /html/canvas/tools/gentest.py.
// OffscreenCanvas test in a worker:2d.filter.canvasFilterObject.convolveMatrix.exceptions
// Description:Test exceptions on CanvasFilter() convolveMatrix
// Note:

importScripts("/resources/testharness.js");
importScripts("/html/canvas/resources/canvas-tests.js");

var t = async_test("Test exceptions on CanvasFilter() convolveMatrix");
var t_pass = t.done.bind(t);
var t_fail = t.step_func(function(reason) {
    throw reason;
});
t.step(function() {

var canvas = new OffscreenCanvas(100, 50);
var ctx = canvas.getContext('2d');

assert_throws_js(TypeError, function() { new CanvasFilter({filter: "convolveMatrix"}); });
assert_throws_js(TypeError, function() { new CanvasFilter({filter: "convolveMatrix", divisor: 2}); });
assert_throws_js(TypeError, function() { new CanvasFilter({filter: "convolveMatrix", kernelMatrix: null}); });
assert_throws_js(TypeError, function() { new CanvasFilter({filter: "convolveMatrix", kernelMatrix: 1}); });
assert_throws_js(TypeError, function() { new CanvasFilter({filter: "convolveMatrix", kernelMatrix: [[1, 0], [0]]}); });
assert_throws_js(TypeError, function() { new CanvasFilter({filter: "convolveMatrix", kernelMatrix: [[1, "a"], [0]]}); });
assert_throws_js(TypeError, function() { new CanvasFilter({filter: "convolveMatrix", kernelMatrix: [[1, 0], 0]}); });
assert_throws_js(TypeError, function() { new CanvasFilter({filter: "convolveMatrix", kernelMatrix: [[1, 0], [0, Infinity]]}); });
assert_throws_js(TypeError, function() { new CanvasFilter({filter: "convolveMatrix", kernelMatrix: []}); });
// This should not throw an error
ctx.filter = new CanvasFilter({filter: "convolveMatrix", kernelMatrix: [[]]});

t.done();

});
done();
