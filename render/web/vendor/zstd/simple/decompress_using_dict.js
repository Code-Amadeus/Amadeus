import { Module } from '../module.js';
import { isError } from '../errors/index.js';
const getFrameContentSize = (src, size) => {
    const getSize = Module['_ZSTD_getFrameContentSize'];
    return getSize(src, size);
};
export const createDCtx = () => {
    return Module['_ZSTD_createDCtx']();
};
export const freeDCtx = (dctx) => {
    return Module['_ZSTD_freeDCtx'](dctx);
};
export const decompressUsingDict = (dctx, buf, dict, opts = { defaultHeapSize: 1024 * 1024 }) => {
    const malloc = Module['_malloc'];
    const src = malloc(buf.byteLength);
    Module.HEAP8.set(buf, src);
    const pdict = malloc(dict.byteLength);
    Module.HEAP8.set(dict, pdict);
    const contentSize = getFrameContentSize(src, buf.byteLength);
    const size = contentSize === -1 ? opts.defaultHeapSize : contentSize;
    const free = Module['_free'];
    const heap = malloc(size);
    try {
        const _decompress = Module['_ZSTD_decompress_usingDict'];
        const sizeOrError = _decompress(dctx, heap, size, src, buf.byteLength, pdict, dict.byteLength);
        if (isError(sizeOrError)) {
            throw new Error(`Failed to compress with code ${sizeOrError}`);
        }
        // Copy buffer
        // Uint8Array.prototype.slice() return copied buffer.
        const data = new Uint8Array(Module.HEAPU8.buffer, heap, sizeOrError).slice();
        free(heap, size);
        free(src, buf.byteLength);
        free(pdict, dict.byteLength);
        return data;
    }
    catch (e) {
        free(heap, size);
        free(src, buf.byteLength);
        free(pdict, dict.byteLength);
        throw e;
    }
};