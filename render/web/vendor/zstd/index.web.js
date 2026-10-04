var __awaiter = (this && this.__awaiter) || function (thisArg, _arguments, P, generator) {
    function adopt(value) { return value instanceof P ? value : new P(function (resolve) { resolve(value); }); }
    return new (P || (P = Promise))(function (resolve, reject) {
        function fulfilled(value) { try { step(generator.next(value)); } catch (e) { reject(e); } }
        function rejected(value) { try { step(generator["throw"](value)); } catch (e) { reject(e); } }
        function step(result) { result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected); }
        step((generator = generator.apply(thisArg, _arguments || [])).next());
    });
};
import { Module, waitInitialized } from './module.js';
export const init = (path) => __awaiter(void 0, void 0, void 0, function* () {
    // @ts-ignore
    const url = new URL(`./zstd.wasm`, import.meta.url).href;
    Module['init'](path !== null && path !== void 0 ? path : url);
    yield waitInitialized();
});
export * from './simple/decompress.js';
export * from './simple/compress.js';
export * from './simple/decompress_using_dict.js';
export * from './simple/compress_using_dict.js';