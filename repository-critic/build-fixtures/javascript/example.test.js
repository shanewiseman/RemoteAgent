"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { add } = require("./example.js");

test("add", () => assert.equal(add(2, 3), 5));
