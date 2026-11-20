"use strict";
(() => {
  var __getOwnPropNames = Object.getOwnPropertyNames;
  var __commonJS = (cb, mod) => function __require() {
    return mod || (0, cb[__getOwnPropNames(cb)[0]])((mod = { exports: {} }).exports, mod), mod.exports;
  };

  // tmp/pixi-global-shim.js
  var require_pixi_global_shim = __commonJS({
    "tmp/pixi-global-shim.js"(exports, module) {
      "use strict";
      module.exports = globalThis.PIXI;
    }
  });

  // tmp/package/lib/cjs/Basis.js
  var require_Basis = __commonJS({
    "tmp/package/lib/cjs/Basis.js"(exports) {
      "use strict";
      Object.defineProperty(exports, "__esModule", { value: true });
      exports.BASIS_FORMATS_ALPHA = exports.INTERNAL_FORMAT_TO_BASIS_FORMAT = exports.BASIS_FORMAT_TO_TYPE = exports.BASIS_FORMAT_TO_INTERNAL_FORMAT = exports.BASIS_FORMATS = void 0;
      var compressed_textures_1 = require_pixi_global_shim();
      var core_1 = require_pixi_global_shim();
      var BASIS_FORMATS;
      (function(BASIS_FORMATS2) {
        BASIS_FORMATS2[BASIS_FORMATS2["cTFETC1"] = 0] = "cTFETC1";
        BASIS_FORMATS2[BASIS_FORMATS2["cTFETC2"] = 1] = "cTFETC2";
        BASIS_FORMATS2[BASIS_FORMATS2["cTFBC1"] = 2] = "cTFBC1";
        BASIS_FORMATS2[BASIS_FORMATS2["cTFBC3"] = 3] = "cTFBC3";
        BASIS_FORMATS2[BASIS_FORMATS2["cTFBC4"] = 4] = "cTFBC4";
        BASIS_FORMATS2[BASIS_FORMATS2["cTFBC5"] = 5] = "cTFBC5";
        BASIS_FORMATS2[BASIS_FORMATS2["cTFBC7"] = 6] = "cTFBC7";
        BASIS_FORMATS2[BASIS_FORMATS2["cTFPVRTC1_4_RGB"] = 8] = "cTFPVRTC1_4_RGB";
        BASIS_FORMATS2[BASIS_FORMATS2["cTFPVRTC1_4_RGBA"] = 9] = "cTFPVRTC1_4_RGBA";
        BASIS_FORMATS2[BASIS_FORMATS2["cTFASTC_4x4"] = 10] = "cTFASTC_4x4";
        BASIS_FORMATS2[BASIS_FORMATS2["cTFATC_RGB"] = 11] = "cTFATC_RGB";
        BASIS_FORMATS2[BASIS_FORMATS2["cTFATC_RGBA_INTERPOLATED_ALPHA"] = 12] = "cTFATC_RGBA_INTERPOLATED_ALPHA";
        BASIS_FORMATS2[BASIS_FORMATS2["cTFRGBA32"] = 13] = "cTFRGBA32";
        BASIS_FORMATS2[BASIS_FORMATS2["cTFRGB565"] = 14] = "cTFRGB565";
        BASIS_FORMATS2[BASIS_FORMATS2["cTFBGR565"] = 15] = "cTFBGR565";
        BASIS_FORMATS2[BASIS_FORMATS2["cTFRGBA4444"] = 16] = "cTFRGBA4444";
      })(BASIS_FORMATS || (exports.BASIS_FORMATS = BASIS_FORMATS = {}));
      exports.BASIS_FORMAT_TO_INTERNAL_FORMAT = {
        [BASIS_FORMATS.cTFETC1]: compressed_textures_1.INTERNAL_FORMATS.COMPRESSED_RGB_ETC1_WEBGL,
        [BASIS_FORMATS.cTFBC1]: compressed_textures_1.INTERNAL_FORMATS.COMPRESSED_RGB_S3TC_DXT1_EXT,
        [BASIS_FORMATS.cTFBC3]: compressed_textures_1.INTERNAL_FORMATS.COMPRESSED_RGBA_S3TC_DXT5_EXT,
        [BASIS_FORMATS.cTFPVRTC1_4_RGB]: compressed_textures_1.INTERNAL_FORMATS.COMPRESSED_RGB_PVRTC_4BPPV1_IMG,
        [BASIS_FORMATS.cTFPVRTC1_4_RGBA]: compressed_textures_1.INTERNAL_FORMATS.COMPRESSED_RGBA_PVRTC_4BPPV1_IMG,
        [BASIS_FORMATS.cTFATC_RGB]: compressed_textures_1.INTERNAL_FORMATS.COMPRESSED_RGB_ATC_WEBGL,
        [BASIS_FORMATS.cTFASTC_4x4]: compressed_textures_1.INTERNAL_FORMATS.COMPRESSED_RGBA_ASTC_4x4_KHR,
        [BASIS_FORMATS.cTFBC7]: compressed_textures_1.INTERNAL_FORMATS.COMPRESSED_RGBA_BPTC_UNORM_EXT
      };
      exports.BASIS_FORMAT_TO_TYPE = {
        [BASIS_FORMATS.cTFRGBA32]: core_1.TYPES.UNSIGNED_BYTE,
        [BASIS_FORMATS.cTFRGB565]: core_1.TYPES.UNSIGNED_SHORT_5_6_5,
        [BASIS_FORMATS.cTFRGBA4444]: core_1.TYPES.UNSIGNED_SHORT_4_4_4_4
      };
      exports.INTERNAL_FORMAT_TO_BASIS_FORMAT = Object.keys(exports.BASIS_FORMAT_TO_INTERNAL_FORMAT).map((key) => Number(key)).reduce((reverseMap, basisFormat) => {
        reverseMap[exports.BASIS_FORMAT_TO_INTERNAL_FORMAT[basisFormat]] = basisFormat;
        return reverseMap;
      }, {});
      exports.BASIS_FORMATS_ALPHA = {
        [BASIS_FORMATS.cTFBC3]: true,
        [BASIS_FORMATS.cTFPVRTC1_4_RGBA]: true,
        [BASIS_FORMATS.cTFASTC_4x4]: true,
        [BASIS_FORMATS.cTFBC7]: true
      };
    }
  });

  // tmp/package/lib/cjs/TranscoderWorkerWrapperBasis.js
  var require_TranscoderWorkerWrapperBasis = __commonJS({
    "tmp/package/lib/cjs/TranscoderWorkerWrapperBasis.js"(exports) {
      "use strict";
      Object.defineProperty(exports, "__esModule", { value: true });
      exports.TranscoderWorkerWrapperBasis = TranscoderWorkerWrapperBasis;
      function TranscoderWorkerWrapperBasis() {
        let basisBinding;
        const messageHandlers = {
          init: (message) => {
            if (!self.BASIS) {
              console.warn("jsSource was not prepended?");
              return {
                type: "init",
                success: false
              };
            }
            void self.BASIS({ wasmBinary: message.wasmSource }).then((basisLibrary) => {
              basisLibrary.initializeBasis();
              basisBinding = basisLibrary;
              self.postMessage({
                type: "init",
                success: true
              });
            });
            return null;
          },
          transcode(message) {
            const basisData = message.basisData;
            const BASIS = basisBinding;
            const data = basisData;
            const basisFile = new BASIS.BasisFile(data);
            const imageCount = basisFile.getNumImages();
            const hasAlpha = basisFile.getHasAlpha();
            const basisFormat = hasAlpha ? message.rgbaFormat : message.rgbFormat;
            const basisFallbackFormat = 14;
            const imageArray = new Array(imageCount);
            let fallbackMode = false;
            if (!basisFile.startTranscoding()) {
              basisFile.close();
              basisFile.delete();
              return {
                type: "transcode",
                requestID: message.requestID,
                success: false,
                imageArray: void 0
              };
            }
            for (let i = 0; i < imageCount; i++) {
              const levels = basisFile.getNumLevels(i);
              const imageResource = {
                imageID: i,
                levelArray: new Array()
              };
              for (let j = 0; j < levels; j++) {
                const format = !fallbackMode ? basisFormat : basisFallbackFormat;
                const width = basisFile.getImageWidth(i, j);
                const height = basisFile.getImageHeight(i, j);
                const byteSize = basisFile.getImageTranscodedSizeInBytes(i, j, format);
                if (j === 0) {
                  const alignedWidth = width + 3 & ~3;
                  const alignedHeight = height + 3 & ~3;
                  imageResource.width = alignedWidth;
                  imageResource.height = alignedHeight;
                }
                const imageBuffer = new Uint8Array(byteSize);
                if (!basisFile.transcodeImage(imageBuffer, i, j, format, false, false)) {
                  if (fallbackMode) {
                    console.error(`Basis failed to transcode image ${i}, level ${j}!`);
                    return { type: "transcode", requestID: message.requestID, success: false };
                  }
                  console.warn(`Basis failed to transcode image ${i}, level ${j}! Retrying to an uncompressed texture format!`);
                  i = -1;
                  fallbackMode = true;
                  break;
                }
                imageResource.levelArray.push({
                  levelID: j,
                  levelWidth: width,
                  levelHeight: height,
                  levelBuffer: imageBuffer
                });
              }
              imageArray[i] = imageResource;
            }
            basisFile.close();
            basisFile.delete();
            return {
              type: "transcode",
              requestID: message.requestID,
              success: true,
              basisFormat: !fallbackMode ? basisFormat : basisFallbackFormat,
              imageArray
            };
          }
        };
        self.onmessage = (e) => {
          const msg = e.data;
          const response = messageHandlers[msg.type](msg);
          if (response) {
            self.postMessage(response);
          }
        };
      }
    }
  });

  // tmp/package/lib/cjs/TranscoderWorkerBasis.js
  var require_TranscoderWorkerBasis = __commonJS({
    "tmp/package/lib/cjs/TranscoderWorkerBasis.js"(exports) {
      "use strict";
      var __awaiter = exports && exports.__awaiter || function(thisArg, _arguments, P, generator) {
        function adopt(value) {
          return value instanceof P ? value : new P(function(resolve) {
            resolve(value);
          });
        }
        return new (P || (P = Promise))(function(resolve, reject) {
          function fulfilled(value) {
            try {
              step(generator.next(value));
            } catch (e) {
              reject(e);
            }
          }
          function rejected(value) {
            try {
              step(generator["throw"](value));
            } catch (e) {
              reject(e);
            }
          }
          function step(result) {
            result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected);
          }
          step((generator = generator.apply(thisArg, _arguments || [])).next());
        });
      };
      Object.defineProperty(exports, "__esModule", { value: true });
      exports.TranscoderWorkerBasis = void 0;
      var TranscoderWorkerWrapperBasis_1 = require_TranscoderWorkerWrapperBasis();
      var TranscoderWorkerBasis = class _TranscoderWorkerBasis {
        /** Generated URL for the transcoder worker script. */
        static get workerURL() {
          if (!_TranscoderWorkerBasis._workerURL) {
            let workerSource = TranscoderWorkerWrapperBasis_1.TranscoderWorkerWrapperBasis.toString();
            const beginIndex = workerSource.indexOf("{");
            const endIndex = workerSource.lastIndexOf("}");
            workerSource = workerSource.slice(beginIndex + 1, endIndex);
            if (_TranscoderWorkerBasis.jsSource) {
              workerSource = `${_TranscoderWorkerBasis.jsSource}
${workerSource}`;
            }
            _TranscoderWorkerBasis._workerURL = URL.createObjectURL(new Blob([workerSource]));
          }
          return _TranscoderWorkerBasis._workerURL;
        }
        constructor() {
          this.requests = {};
          this.onInit = () => {
          };
          this.onMessage = (e) => {
            const data = e.data;
            if (data.type === "init") {
              if (!data.success) {
                throw new Error("BasisResource.TranscoderWorker failed to initialize.");
              }
              this.isInit = true;
              this.onInit();
            } else if (data.type === "transcode") {
              --this.load;
              const requestID = data.requestID;
              if (data.success) {
                this.requests[requestID].resolve(data);
              } else {
                this.requests[requestID].reject();
              }
              delete this.requests[requestID];
            }
          };
          this.isInit = false;
          this.load = 0;
          this.initPromise = new Promise((resolve) => {
            this.onInit = resolve;
          });
          if (!_TranscoderWorkerBasis.wasmSource) {
            console.warn("resources.BasisResource.TranscoderWorker has not been given the transcoder WASM binary!");
          }
          this.worker = new Worker(_TranscoderWorkerBasis.workerURL);
          this.worker.onmessage = this.onMessage;
          this.worker.postMessage({
            type: "init",
            jsSource: _TranscoderWorkerBasis.jsSource,
            wasmSource: _TranscoderWorkerBasis.wasmSource
          });
        }
        /** @returns a promise that is resolved when the web-worker is initialized */
        initAsync() {
          return this.initPromise;
        }
        /**
         * Creates a promise that will resolve when the transcoding of a *.basis file is complete.
         * @param basisData - *.basis file contents
         * @param rgbaFormat - transcoding format for RGBA files
         * @param rgbFormat - transcoding format for RGB files
         * @returns a promise that is resolved with the transcoding response of the web-worker
         */
        transcodeAsync(basisData, rgbaFormat, rgbFormat) {
          return __awaiter(this, void 0, void 0, function* () {
            ++this.load;
            const requestID = _TranscoderWorkerBasis._tempID++;
            const requestPromise = new Promise((resolve, reject) => {
              this.requests[requestID] = {
                resolve,
                reject
              };
            });
            this.worker.postMessage({
              requestID,
              basisData,
              rgbaFormat,
              rgbFormat,
              type: "transcode"
            });
            return requestPromise;
          });
        }
        /**
         * Loads the transcoder source code
         * @param jsURL - URL to the javascript basis transcoder
         * @param wasmURL - URL to the wasm basis transcoder
         * @returns A promise that resolves when both the js and wasm transcoders have been loaded.
         */
        static loadTranscoder(jsURL, wasmURL) {
          const jsPromise = fetch(jsURL).then((res) => res.text()).then((text) => {
            _TranscoderWorkerBasis.jsSource = text;
          });
          const wasmPromise = fetch(wasmURL).then((res) => res.arrayBuffer()).then((arrayBuffer) => {
            _TranscoderWorkerBasis.wasmSource = arrayBuffer;
          });
          return Promise.all([jsPromise, wasmPromise]).then((data) => {
            this._onTranscoderInitializedResolve();
            return data;
          });
        }
        /**
         * Set the transcoder source code directly
         * @param jsSource - source for the javascript basis transcoder
         * @param wasmSource - source for the wasm basis transcoder
         */
        static setTranscoder(jsSource, wasmSource) {
          _TranscoderWorkerBasis.jsSource = jsSource;
          _TranscoderWorkerBasis.wasmSource = wasmSource;
        }
      };
      exports.TranscoderWorkerBasis = TranscoderWorkerBasis;
      TranscoderWorkerBasis.onTranscoderInitialized = new Promise((resolve) => {
        TranscoderWorkerBasis._onTranscoderInitializedResolve = resolve;
      });
      TranscoderWorkerBasis._tempID = 0;
    }
  });

  // tmp/package/lib/cjs/loader/BasisParser.js
  var require_BasisParser = __commonJS({
    "tmp/package/lib/cjs/loader/BasisParser.js"(exports) {
      "use strict";
      var __awaiter = exports && exports.__awaiter || function(thisArg, _arguments, P, generator) {
        function adopt(value) {
          return value instanceof P ? value : new P(function(resolve) {
            resolve(value);
          });
        }
        return new (P || (P = Promise))(function(resolve, reject) {
          function fulfilled(value) {
            try {
              step(generator.next(value));
            } catch (e) {
              reject(e);
            }
          }
          function rejected(value) {
            try {
              step(generator["throw"](value));
            } catch (e) {
              reject(e);
            }
          }
          function step(result) {
            result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected);
          }
          step((generator = generator.apply(thisArg, _arguments || [])).next());
        });
      };
      Object.defineProperty(exports, "__esModule", { value: true });
      exports.BasisParser = void 0;
      var compressed_textures_1 = require_pixi_global_shim();
      var core_1 = require_pixi_global_shim();
      var Basis_1 = require_Basis();
      var TranscoderWorkerBasis_1 = require_TranscoderWorkerBasis();
      var BasisParser = class _BasisParser {
        /**
         * Runs transcoding and populates {@link imageArray}. It will run the transcoding in a web worker
         * if they are available.
         * @private
         */
        static transcode(arrayBuffer) {
          return __awaiter(this, void 0, void 0, function* () {
            let resources;
            if (typeof Worker !== "undefined" && _BasisParser.TranscoderWorker.wasmSource) {
              resources = yield _BasisParser.transcodeAsync(arrayBuffer);
            } else {
              resources = _BasisParser.transcodeSync(arrayBuffer);
            }
            return resources;
          });
        }
        /**
         * Finds a suitable worker for transcoding and sends a transcoding request
         * @private
         * @async
         */
        static transcodeAsync(arrayBuffer) {
          return __awaiter(this, void 0, void 0, function* () {
            var _a, _b;
            if (!_BasisParser.defaultRGBAFormat && !_BasisParser.defaultRGBFormat) {
              _BasisParser.autoDetectFormats();
            }
            const workerPool = _BasisParser.workerPool;
            let leastLoad = 268435456;
            let worker = null;
            for (let i = 0, j = workerPool.length; i < j; i++) {
              if (workerPool[i].load < leastLoad) {
                worker = workerPool[i];
                leastLoad = worker.load;
              }
            }
            if (!worker) {
              worker = new TranscoderWorkerBasis_1.TranscoderWorkerBasis();
              workerPool.push(worker);
            }
            yield worker.initAsync();
            const response = yield worker.transcodeAsync(new Uint8Array(arrayBuffer), _BasisParser.defaultRGBAFormat.basisFormat, _BasisParser.defaultRGBFormat.basisFormat);
            const basisFormat = (_a = response.basisFormat) !== null && _a !== void 0 ? _a : 13;
            const imageArray = (_b = response.imageArray) !== null && _b !== void 0 ? _b : [];
            const fallbackMode = Number(basisFormat) > 12;
            let imageResources;
            if (!fallbackMode) {
              const format = Basis_1.BASIS_FORMAT_TO_INTERNAL_FORMAT[basisFormat];
              imageResources = new Array(imageArray.length);
              for (let i = 0, j = imageArray.length; i < j; i++) {
                imageResources[i] = new compressed_textures_1.CompressedTextureResource(null, {
                  format,
                  width: imageArray[i].width,
                  height: imageArray[i].height,
                  levelBuffers: imageArray[i].levelArray,
                  levels: imageArray[i].levelArray.length
                });
              }
            } else {
              imageResources = imageArray.map((image) => new core_1.BufferResource(new Uint16Array(image.levelArray[0].levelBuffer.buffer), {
                width: image.width,
                height: image.height
              }));
            }
            imageResources.basisFormat = basisFormat;
            return imageResources;
          });
        }
        /**
         * Runs transcoding on the main thread.
         * @private
         */
        static transcodeSync(arrayBuffer) {
          if (!_BasisParser.defaultRGBAFormat && !_BasisParser.defaultRGBFormat) {
            _BasisParser.autoDetectFormats();
          }
          const BASIS = _BasisParser.basisBinding;
          const data = new Uint8Array(arrayBuffer);
          const basisFile = new BASIS.BasisFile(data);
          const imageCount = basisFile.getNumImages();
          const hasAlpha = basisFile.getHasAlpha();
          const basisFormat = hasAlpha ? _BasisParser.defaultRGBAFormat.basisFormat : _BasisParser.defaultRGBFormat.basisFormat;
          const basisFallbackFormat = Basis_1.BASIS_FORMATS.cTFRGB565;
          const imageResources = new Array(imageCount);
          let fallbackMode = _BasisParser.fallbackMode;
          if (!basisFile.startTranscoding()) {
            console.error(`Basis failed to start transcoding!`);
            basisFile.close();
            basisFile.delete();
            return null;
          }
          for (let i = 0; i < imageCount; i++) {
            const levels = !fallbackMode ? basisFile.getNumLevels(i) : 1;
            const width = basisFile.getImageWidth(i, 0);
            const height = basisFile.getImageHeight(i, 0);
            const alignedWidth = width + 3 & ~3;
            const alignedHeight = height + 3 & ~3;
            const imageLevels = new Array(levels);
            for (let j = 0; j < levels; j++) {
              const levelWidth = basisFile.getImageWidth(i, j);
              const levelHeight = basisFile.getImageHeight(i, j);
              const byteSize = basisFile.getImageTranscodedSizeInBytes(i, 0, !fallbackMode ? basisFormat : basisFallbackFormat);
              imageLevels[j] = {
                levelID: j,
                levelBuffer: new Uint8Array(byteSize),
                levelWidth,
                levelHeight
              };
              if (!basisFile.transcodeImage(imageLevels[j].levelBuffer, i, 0, !fallbackMode ? basisFormat : basisFallbackFormat, false, false)) {
                if (fallbackMode) {
                  console.error(`Basis failed to transcode image ${i}, level ${0}!`);
                  break;
                } else {
                  i = -1;
                  fallbackMode = true;
                  console.warn(`Basis failed to transcode image ${i}, level ${0} to a compressed texture format. Retrying to an uncompressed fallback format!`);
                  continue;
                }
              }
            }
            let imageResource;
            if (!fallbackMode) {
              imageResource = new compressed_textures_1.CompressedTextureResource(null, {
                format: Basis_1.BASIS_FORMAT_TO_INTERNAL_FORMAT[basisFormat],
                width: alignedWidth,
                height: alignedHeight,
                levelBuffers: imageLevels,
                levels
              });
            } else {
              imageResource = new core_1.BufferResource(new Uint16Array(imageLevels[0].levelBuffer.buffer), { width, height });
            }
            imageResources[i] = imageResource;
          }
          basisFile.close();
          basisFile.delete();
          const transcodedResources = imageResources;
          transcodedResources.basisFormat = !fallbackMode ? basisFormat : basisFallbackFormat;
          return transcodedResources;
        }
        /**
         * Detects the available compressed texture formats on the device.
         * @param extensions - extensions provided by a WebGL context
         * @ignore
         */
        static autoDetectFormats(extensions) {
          var _a, _b, _c, _d, _e, _f, _g, _h, _j;
          console.log("autoDetectFormats", extensions);
          if (!extensions) {
            const canvas = core_1.settings.ADAPTER.createCanvas();
            const gl = canvas.getContext("webgl");
            if (!gl) {
              console.error("WebGL not available for BASIS transcoding. Silently failing.");
              return;
            }
            extensions = {
              bptc: (_a = gl.getExtension("EXT_texture_compression_bptc")) !== null && _a !== void 0 ? _a : void 0,
              astc: (_b = gl.getExtension("WEBGL_compressed_texture_astc")) !== null && _b !== void 0 ? _b : void 0,
              etc: (_c = gl.getExtension("WEBGL_compressed_texture_etc")) !== null && _c !== void 0 ? _c : void 0,
              s3tc: (_d = gl.getExtension("WEBGL_compressed_texture_s3tc")) !== null && _d !== void 0 ? _d : void 0,
              s3tc_sRGB: (_e = gl.getExtension("WEBGL_compressed_texture_s3tc_srgb")) !== null && _e !== void 0 ? _e : void 0,
              pvrtc: (_f = gl.getExtension("WEBGL_compressed_texture_pvrtc") || gl.getExtension("WEBKIT_WEBGL_compressed_texture_pvrtc")) !== null && _f !== void 0 ? _f : void 0,
              etc1: (_g = gl.getExtension("WEBGL_compressed_texture_etc1")) !== null && _g !== void 0 ? _g : void 0,
              atc: (_h = gl.getExtension("WEBGL_compressed_texture_atc")) !== null && _h !== void 0 ? _h : void 0
            };
          }
          const supportedFormats = {};
          for (const key in extensions) {
            const extension = extensions[key];
            if (!extension) {
              continue;
            }
            Object.assign(supportedFormats, Object.getPrototypeOf(extension));
          }
          for (let i = 0; i < 2; i++) {
            const detectWithAlpha = !!i;
            let internalFormat = 0;
            let basisFormat = Basis_1.BASIS_FORMATS.cTFRGB565;
            for (const id in supportedFormats) {
              internalFormat = (_j = supportedFormats[id]) !== null && _j !== void 0 ? _j : 0;
              basisFormat = Basis_1.INTERNAL_FORMAT_TO_BASIS_FORMAT[internalFormat];
              if (basisFormat !== void 0) {
                if (detectWithAlpha && Basis_1.BASIS_FORMATS_ALPHA[basisFormat] || !detectWithAlpha && !Basis_1.BASIS_FORMATS_ALPHA[basisFormat]) {
                  break;
                }
              }
            }
            if (internalFormat) {
              _BasisParser[detectWithAlpha ? "defaultRGBAFormat" : "defaultRGBFormat"] = {
                textureFormat: internalFormat,
                basisFormat
              };
            } else {
              _BasisParser[detectWithAlpha ? "defaultRGBAFormat" : "defaultRGBFormat"] = {
                textureFormat: core_1.TYPES.UNSIGNED_SHORT_5_6_5,
                basisFormat: Basis_1.BASIS_FORMATS.cTFRGB565
              };
              _BasisParser.fallbackMode = true;
            }
          }
        }
        /**
         * Binds the basis_universal transcoder to decompress *.basis files. You must initialize the transcoder library yourself.
         * @example
         * import { BasisParser } from 'pixi-basis-ktx2';
         *
         * // BASIS() returns a Promise-like object
         * globalThis.BASIS().then((basisLibrary) =>
         * {
         *     // Initialize basis-library; otherwise, transcoded results maybe corrupt!
         *     basisLibrary.initializeBasis();
         *
         *     // Bind BasisParser to the transcoder
         *     BasisParser.bindTranscoder(basisLibrary);
         * });
         * @param basisLibrary - the initialized transcoder library
         * @private
         */
        static bindTranscoder(basisLibrary) {
          _BasisParser.basisBinding = basisLibrary;
        }
        /**
         * Loads the transcoder source code for use in {@link PIXI.BasisParser.TranscoderWorker}.
         * @private
         * @param jsURL - URL to the javascript basis transcoder
         * @param wasmURL - URL to the wasm basis transcoder
         */
        static loadTranscoder(jsURL, wasmURL) {
          return _BasisParser.TranscoderWorker.loadTranscoder(jsURL, wasmURL);
        }
        /**
         * Set the transcoder source code directly
         * @private
         * @param jsSource - source for the javascript basis transcoder
         * @param wasmSource - source for the wasm basis transcoder
         */
        static setTranscoder(jsSource, wasmSource) {
          _BasisParser.TranscoderWorker.setTranscoder(jsSource, wasmSource);
        }
        static get TRANSCODER_WORKER_POOL_LIMIT() {
          return this.workerPool.length || 1;
        }
        static set TRANSCODER_WORKER_POOL_LIMIT(limit) {
          for (let i = this.workerPool.length; i < limit; i++) {
            this.workerPool[i] = new TranscoderWorkerBasis_1.TranscoderWorkerBasis();
            void this.workerPool[i].initAsync();
          }
        }
      };
      exports.BasisParser = BasisParser;
      BasisParser.fallbackMode = false;
      BasisParser.workerPool = [];
      BasisParser.TranscoderWorker = TranscoderWorkerBasis_1.TranscoderWorkerBasis;
    }
  });

  // tmp/package/lib/cjs/loader/detectBasis.js
  var require_detectBasis = __commonJS({
    "tmp/package/lib/cjs/loader/detectBasis.js"(exports) {
      "use strict";
      var __awaiter = exports && exports.__awaiter || function(thisArg, _arguments, P, generator) {
        function adopt(value) {
          return value instanceof P ? value : new P(function(resolve) {
            resolve(value);
          });
        }
        return new (P || (P = Promise))(function(resolve, reject) {
          function fulfilled(value) {
            try {
              step(generator.next(value));
            } catch (e) {
              reject(e);
            }
          }
          function rejected(value) {
            try {
              step(generator["throw"](value));
            } catch (e) {
              reject(e);
            }
          }
          function step(result) {
            result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected);
          }
          step((generator = generator.apply(thisArg, _arguments || [])).next());
        });
      };
      Object.defineProperty(exports, "__esModule", { value: true });
      exports.detectBasis = void 0;
      var core_1 = require_pixi_global_shim();
      var BasisParser_1 = require_BasisParser();
      exports.detectBasis = {
        extension: {
          type: core_1.ExtensionType.DetectionParser,
          priority: 3
        },
        test: () => __awaiter(void 0, void 0, void 0, function* () {
          return !!(BasisParser_1.BasisParser.basisBinding && BasisParser_1.BasisParser.TranscoderWorker.wasmSource);
        }),
        add: (formats) => __awaiter(void 0, void 0, void 0, function* () {
          return [...formats, "basis"];
        }),
        remove: (formats) => __awaiter(void 0, void 0, void 0, function* () {
          return formats.filter((f) => f !== "basis");
        })
      };
      core_1.extensions.add(exports.detectBasis);
    }
  });

  // tmp/package/lib/cjs/TranscoderWorkerWrapperKTX2.js
  var require_TranscoderWorkerWrapperKTX2 = __commonJS({
    "tmp/package/lib/cjs/TranscoderWorkerWrapperKTX2.js"(exports) {
      "use strict";
      Object.defineProperty(exports, "__esModule", { value: true });
      exports.TranscoderWorkerWrapperKTX2 = TranscoderWorkerWrapperKTX2;
      function TranscoderWorkerWrapperKTX2() {
        let KTX2Binding;
        const messageHandlers = {
          init: (message) => {
            if (!self.BASIS) {
              console.warn("jsSource was not prepended?");
              return {
                type: "init",
                success: false
              };
            }
            void self.BASIS({ wasmBinary: message.wasmSource }).then((basisLibrary) => {
              basisLibrary.initializeBasis();
              KTX2Binding = basisLibrary;
              self.postMessage({
                type: "init",
                success: true
              });
            });
            return null;
          },
          transcode(message) {
            const basisData = message.basisData;
            const BASIS = KTX2Binding;
            const data = basisData;
            const ktx2File = new BASIS.KTX2File(data);
            const imageCount = ktx2File.getLevels() * Math.max(1, ktx2File.getLayers()) * ktx2File.getFaces();
            let levels = ktx2File.getLevels();
            const layers = ktx2File.getLayers();
            const faces = ktx2File.getFaces();
            const hasAlpha = ktx2File.getHasAlpha();
            const basisFormat = hasAlpha ? message.rgbaFormat : message.rgbFormat;
            const basisFallbackFormat = 14;
            const imageArray = new Array(imageCount);
            let fallbackMode = false;
            if (!ktx2File.startTranscoding()) {
              ktx2File.close();
              ktx2File.delete();
              return {
                type: "transcode",
                requestID: message.requestID,
                success: false
              };
            }
            for (let i = 0; i < levels; i++) {
              const imageResource = {
                imageID: i,
                levelArray: new Array()
              };
              for (let j = 0; j < Math.max(1, layers); j++) {
                for (let k = 0; k < faces; k++) {
                  const imageLevelInfo = ktx2File.getImageLevelInfo(i, j, k);
                  const width = imageLevelInfo.width;
                  const height = imageLevelInfo.height;
                  const format = !fallbackMode ? basisFormat : basisFallbackFormat;
                  const byteSize = ktx2File.getImageTranscodedSizeInBytes(i, j, k, format);
                  if (j === 0) {
                    const alignedWidth = width + 3 & ~3;
                    const alignedHeight = height + 3 & ~3;
                    imageResource.width = alignedWidth;
                    imageResource.height = alignedHeight;
                  }
                  const imageBuffer = new Uint8Array(byteSize);
                  if (!ktx2File.transcodeImage(imageBuffer, i, j, k, format, false, -1, -1)) {
                    if (fallbackMode) {
                      console.error(`Basis failed to transcode image ${i}, level ${j}!`);
                      return { type: "transcode", requestID: message.requestID, success: false };
                    }
                    console.warn(`Basis failed to transcode image ${i}, level ${j}! Retrying to an uncompressed texture format!`);
                    i = -1;
                    levels = 1;
                    fallbackMode = true;
                    break;
                  }
                  imageResource.levelArray.push({
                    levelID: j,
                    levelWidth: width,
                    levelHeight: height,
                    levelBuffer: imageBuffer
                  });
                }
              }
              imageArray[i] = imageResource;
            }
            ktx2File.close();
            ktx2File.delete();
            return {
              type: "transcode",
              requestID: message.requestID,
              success: true,
              basisFormat: !fallbackMode ? basisFormat : basisFallbackFormat,
              imageArray
            };
          }
        };
        self.onmessage = (e) => {
          const msg = e.data;
          const response = messageHandlers[msg.type](msg);
          if (response) {
            self.postMessage(response);
          }
        };
      }
    }
  });

  // tmp/package/lib/cjs/TranscoderWorkerKTX2.js
  var require_TranscoderWorkerKTX2 = __commonJS({
    "tmp/package/lib/cjs/TranscoderWorkerKTX2.js"(exports) {
      "use strict";
      var __awaiter = exports && exports.__awaiter || function(thisArg, _arguments, P, generator) {
        function adopt(value) {
          return value instanceof P ? value : new P(function(resolve) {
            resolve(value);
          });
        }
        return new (P || (P = Promise))(function(resolve, reject) {
          function fulfilled(value) {
            try {
              step(generator.next(value));
            } catch (e) {
              reject(e);
            }
          }
          function rejected(value) {
            try {
              step(generator["throw"](value));
            } catch (e) {
              reject(e);
            }
          }
          function step(result) {
            result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected);
          }
          step((generator = generator.apply(thisArg, _arguments || [])).next());
        });
      };
      Object.defineProperty(exports, "__esModule", { value: true });
      exports.TranscoderWorkerKTX2 = void 0;
      var TranscoderWorkerWrapperKTX2_1 = require_TranscoderWorkerWrapperKTX2();
      var TranscoderWorkerKTX2 = class _TranscoderWorkerKTX2 {
        /** Generated URL for the transcoder worker script. */
        static get workerURL() {
          if (!_TranscoderWorkerKTX2._workerURL) {
            let workerSource = TranscoderWorkerWrapperKTX2_1.TranscoderWorkerWrapperKTX2.toString();
            const beginIndex = workerSource.indexOf("{");
            const endIndex = workerSource.lastIndexOf("}");
            workerSource = workerSource.slice(beginIndex + 1, endIndex);
            if (_TranscoderWorkerKTX2.jsSource) {
              workerSource = `${_TranscoderWorkerKTX2.jsSource}
${workerSource}`;
            }
            _TranscoderWorkerKTX2._workerURL = URL.createObjectURL(new Blob([workerSource]));
          }
          return _TranscoderWorkerKTX2._workerURL;
        }
        constructor() {
          this.requests = {};
          this.onInit = () => {
          };
          this.onMessage = (e) => {
            const data = e.data;
            if (data.type === "init") {
              if (!data.success) {
                throw new Error("BasisResource.TranscoderWorker failed to initialize.");
              }
              this.isInit = true;
              this.onInit();
            } else if (data.type === "transcode") {
              --this.load;
              const requestID = data.requestID;
              if (data.success) {
                this.requests[requestID].resolve(data);
              } else {
                this.requests[requestID].reject();
              }
              delete this.requests[requestID];
            }
          };
          this.isInit = false;
          this.load = 0;
          this.initPromise = new Promise((resolve) => {
            this.onInit = resolve;
          });
          if (!_TranscoderWorkerKTX2.wasmSource) {
            console.warn("resources.BasisResource.TranscoderWorker has not been given the transcoder WASM binary!");
          }
          this.worker = new Worker(_TranscoderWorkerKTX2.workerURL);
          this.worker.onmessage = this.onMessage;
          this.worker.postMessage({
            type: "init",
            jsSource: _TranscoderWorkerKTX2.jsSource,
            wasmSource: _TranscoderWorkerKTX2.wasmSource
          });
        }
        /** @returns a promise that is resolved when the web-worker is initialized */
        initAsync() {
          return this.initPromise;
        }
        /**
         * Creates a promise that will resolve when the transcoding of a *.basis file is complete.
         * @param basisData - *.basis file contents
         * @param rgbaFormat - transcoding format for RGBA files
         * @param rgbFormat - transcoding format for RGB files
         * @returns a promise that is resolved with the transcoding response of the web-worker
         */
        transcodeAsync(basisData, rgbaFormat, rgbFormat) {
          return __awaiter(this, void 0, void 0, function* () {
            ++this.load;
            const requestID = _TranscoderWorkerKTX2._tempID++;
            const requestPromise = new Promise((resolve, reject) => {
              this.requests[requestID] = {
                resolve,
                reject
              };
            });
            this.worker.postMessage({
              requestID,
              basisData,
              rgbaFormat,
              rgbFormat,
              type: "transcode"
            });
            return requestPromise;
          });
        }
        /**
         * Loads the transcoder source code
         * @param jsURL - URL to the javascript basis transcoder
         * @param wasmURL - URL to the wasm basis transcoder
         * @returns A promise that resolves when both the js and wasm transcoders have been loaded.
         */
        static loadTranscoder(jsURL, wasmURL) {
          const jsPromise = fetch(jsURL).then((res) => res.text()).then((text) => {
            _TranscoderWorkerKTX2.jsSource = text;
          });
          const wasmPromise = fetch(wasmURL).then((res) => res.arrayBuffer()).then((arrayBuffer) => {
            _TranscoderWorkerKTX2.wasmSource = arrayBuffer;
          });
          return Promise.all([jsPromise, wasmPromise]).then((data) => {
            this._onTranscoderInitializedResolve();
            return data;
          });
        }
        /**
         * Set the transcoder source code directly
         * @param jsSource - source for the javascript basis transcoder
         * @param wasmSource - source for the wasm basis transcoder
         */
        static setTranscoder(jsSource, wasmSource) {
          _TranscoderWorkerKTX2.jsSource = jsSource;
          _TranscoderWorkerKTX2.wasmSource = wasmSource;
        }
      };
      exports.TranscoderWorkerKTX2 = TranscoderWorkerKTX2;
      TranscoderWorkerKTX2.onTranscoderInitialized = new Promise((resolve) => {
        TranscoderWorkerKTX2._onTranscoderInitializedResolve = resolve;
      });
      TranscoderWorkerKTX2._tempID = 0;
    }
  });

  // tmp/package/lib/cjs/loader/KTX2Parser.js
  var require_KTX2Parser = __commonJS({
    "tmp/package/lib/cjs/loader/KTX2Parser.js"(exports) {
      "use strict";
      var __awaiter = exports && exports.__awaiter || function(thisArg, _arguments, P, generator) {
        function adopt(value) {
          return value instanceof P ? value : new P(function(resolve) {
            resolve(value);
          });
        }
        return new (P || (P = Promise))(function(resolve, reject) {
          function fulfilled(value) {
            try {
              step(generator.next(value));
            } catch (e) {
              reject(e);
            }
          }
          function rejected(value) {
            try {
              step(generator["throw"](value));
            } catch (e) {
              reject(e);
            }
          }
          function step(result) {
            result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected);
          }
          step((generator = generator.apply(thisArg, _arguments || [])).next());
        });
      };
      Object.defineProperty(exports, "__esModule", { value: true });
      exports.KTX2Parser = void 0;
      var compressed_textures_1 = require_pixi_global_shim();
      var core_1 = require_pixi_global_shim();
      var Basis_1 = require_Basis();
      var TranscoderWorkerKTX2_1 = require_TranscoderWorkerKTX2();
      var KTX2Parser = class _KTX2Parser {
        /**
         * Runs transcoding and populates {@link imageArray}. It will run the transcoding in a web worker
         * if they are available.
         * @private
         */
        static transcode(arrayBuffer) {
          return __awaiter(this, void 0, void 0, function* () {
            let resources;
            if (typeof Worker !== "undefined" && _KTX2Parser.TranscoderWorker.wasmSource) {
              resources = yield _KTX2Parser.transcodeAsync(arrayBuffer);
            } else {
              resources = _KTX2Parser.transcodeSync(arrayBuffer);
            }
            return resources;
          });
        }
        /**
         * Finds a suitable worker for transcoding and sends a transcoding request
         * @private
         * @async
         */
        static transcodeAsync(arrayBuffer) {
          return __awaiter(this, void 0, void 0, function* () {
            var _a, _b;
            if (!_KTX2Parser.defaultRGBAFormat && !_KTX2Parser.defaultRGBFormat) {
              _KTX2Parser.autoDetectFormats();
            }
            const workerPool = _KTX2Parser.workerPool;
            let leastLoad = 268435456;
            let worker = null;
            for (let i = 0, j = workerPool.length; i < j; i++) {
              if (workerPool[i].load < leastLoad) {
                worker = workerPool[i];
                leastLoad = worker.load;
              }
            }
            if (!worker) {
              worker = new TranscoderWorkerKTX2_1.TranscoderWorkerKTX2();
              workerPool.push(worker);
            }
            yield worker.initAsync();
            const response = yield worker.transcodeAsync(new Uint8Array(arrayBuffer), _KTX2Parser.defaultRGBAFormat.basisFormat, _KTX2Parser.defaultRGBFormat.basisFormat);
            const basisFormat = (_a = response.basisFormat) !== null && _a !== void 0 ? _a : 13;
            const imageArray = (_b = response.imageArray) !== null && _b !== void 0 ? _b : [];
            const fallbackMode = Number(basisFormat) > 12;
            let imageResources;
            if (!fallbackMode) {
              const format = Basis_1.BASIS_FORMAT_TO_INTERNAL_FORMAT[response.basisFormat];
              imageResources = new Array(imageArray.length);
              for (let i = 0, j = imageArray.length; i < j; i++) {
                imageResources[i] = new compressed_textures_1.CompressedTextureResource(null, {
                  format,
                  width: imageArray[i].width,
                  height: imageArray[i].height,
                  levelBuffers: imageArray[i].levelArray,
                  levels: imageArray[i].levelArray.length
                });
              }
            } else {
              imageResources = imageArray.map((image) => new core_1.BufferResource(new Uint16Array(image.levelArray[0].levelBuffer.buffer), {
                width: image.width,
                height: image.height
              }));
            }
            imageResources.basisFormat = basisFormat;
            return imageResources;
          });
        }
        /**
         * Runs transcoding on the main thread.
         * @private
         */
        static transcodeSync(arrayBuffer) {
          if (!_KTX2Parser.defaultRGBAFormat && !_KTX2Parser.defaultRGBFormat) {
            _KTX2Parser.autoDetectFormats();
          }
          const BASIS = _KTX2Parser.ktx2Binding;
          let fallbackMode = _KTX2Parser.fallbackMode;
          const data = new Uint8Array(arrayBuffer);
          const ktx2File = new BASIS.KTX2File(data);
          const dfdSize = ktx2File.getDFDSize();
          const dvdData = new Uint8Array(dfdSize);
          ktx2File.getDFD(dvdData);
          const levels = !fallbackMode ? ktx2File.getLevels() : 1;
          const layers = ktx2File.getLayers();
          const faces = ktx2File.getFaces();
          const hasAlpha = ktx2File.getHasAlpha();
          const imageLevels = new Array(levels);
          const basisFormat = hasAlpha ? _KTX2Parser.defaultRGBAFormat.basisFormat : _KTX2Parser.defaultRGBFormat.basisFormat;
          const basisFallbackFormat = Basis_1.BASIS_FORMATS.cTFRGB565;
          const imageResources = new Array(levels);
          if (!ktx2File.startTranscoding()) {
            console.error(`Basis failed to start transcoding!`);
            ktx2File.close();
            ktx2File.delete();
            return null;
          }
          for (let i = 0; i < levels; i++) {
            const firstLevel = ktx2File.getImageLevelInfo(i, 0, 0);
            const width = firstLevel.origWidth;
            const height = firstLevel.origHeight;
            const alignedWidth = width + 3 & ~3;
            const alignedHeight = height + 3 & ~3;
            for (let j = 0; j < Math.max(1, layers); j++) {
              for (let k = 0; k < faces; k++) {
                const imageLevelInfo = ktx2File.getImageLevelInfo(i, j, k);
                const levelWidth = imageLevelInfo.width;
                const levelHeight = imageLevelInfo.height;
                const byteSize = ktx2File.getImageTranscodedSizeInBytes(i, j, k, !fallbackMode ? basisFormat : basisFallbackFormat);
                imageLevels[j] = {
                  levelID: j,
                  levelBuffer: new Uint8Array(byteSize),
                  levelWidth,
                  levelHeight
                };
                if (!ktx2File.transcodeImage(
                  // eslint-disable-next-line max-len
                  imageLevels[j].levelBuffer,
                  i,
                  j,
                  k,
                  !fallbackMode ? basisFormat : basisFallbackFormat,
                  false,
                  -1,
                  -1
                )) {
                  if (fallbackMode) {
                    console.error(`Basis failed to transcode image ${i}, level ${0}!`);
                    break;
                  } else {
                    i = -1;
                    fallbackMode = true;
                    console.warn(`Basis failed to transcode image ${i}, level ${0} to a compressed texture format. Retrying to an uncompressed fallback format!`);
                    continue;
                  }
                }
              }
            }
            let imageResource;
            if (!fallbackMode) {
              imageResource = new compressed_textures_1.CompressedTextureResource(null, {
                format: Basis_1.BASIS_FORMAT_TO_INTERNAL_FORMAT[basisFormat],
                width: alignedWidth,
                height: alignedHeight,
                levelBuffers: imageLevels,
                levels
              });
            } else {
              imageResource = new core_1.BufferResource(new Uint16Array(imageLevels[0].levelBuffer.buffer), { width, height });
            }
            imageResources[i] = imageResource;
          }
          ktx2File.close();
          ktx2File.delete();
          const transcodedResources = imageResources;
          transcodedResources.basisFormat = !fallbackMode ? basisFormat : basisFallbackFormat;
          return transcodedResources;
        }
        /**
         * Detects the available compressed texture formats on the device.
         * @param extensions - extensions provided by a WebGL context
         * @ignore
         */
        static autoDetectFormats(extensions) {
          var _a, _b, _c, _d, _e, _f, _g, _h;
          if (!extensions) {
            const canvas = core_1.settings.ADAPTER.createCanvas();
            const gl = canvas.getContext("webgl");
            if (!gl) {
              console.error("WebGL not available for BASIS transcoding. Silently failing.");
              return;
            }
            extensions = {
              bptc: (_a = gl.getExtension("EXT_texture_compression_bptc")) !== null && _a !== void 0 ? _a : void 0,
              astc: (_b = gl.getExtension("WEBGL_compressed_texture_astc")) !== null && _b !== void 0 ? _b : void 0,
              etc: (_c = gl.getExtension("WEBGL_compressed_texture_etc")) !== null && _c !== void 0 ? _c : void 0,
              s3tc: (_d = gl.getExtension("WEBGL_compressed_texture_s3tc")) !== null && _d !== void 0 ? _d : void 0,
              s3tc_sRGB: (_e = gl.getExtension("WEBGL_compressed_texture_s3tc_srgb")) !== null && _e !== void 0 ? _e : void 0,
              pvrtc: (_f = gl.getExtension("WEBGL_compressed_texture_pvrtc") || gl.getExtension("WEBKIT_WEBGL_compressed_texture_pvrtc")) !== null && _f !== void 0 ? _f : void 0,
              etc1: (_g = gl.getExtension("WEBGL_compressed_texture_etc1")) !== null && _g !== void 0 ? _g : void 0,
              atc: (_h = gl.getExtension("WEBGL_compressed_texture_atc")) !== null && _h !== void 0 ? _h : void 0
            };
          }
          const supportedFormats = {};
          for (const key in extensions) {
            const extension = extensions[key];
            if (!extension) {
              continue;
            }
            Object.assign(supportedFormats, Object.getPrototypeOf(extension));
          }
          for (let i = 0; i < 2; i++) {
            const detectWithAlpha = !!i;
            let internalFormat = void 0;
            let basisFormat = Basis_1.BASIS_FORMATS.cTFRGB565;
            for (const id in supportedFormats) {
              internalFormat = supportedFormats[id];
              basisFormat = Basis_1.INTERNAL_FORMAT_TO_BASIS_FORMAT[internalFormat];
              if (basisFormat !== void 0) {
                if (detectWithAlpha && Basis_1.BASIS_FORMATS_ALPHA[basisFormat] || !detectWithAlpha && !Basis_1.BASIS_FORMATS_ALPHA[basisFormat]) {
                  break;
                }
              }
            }
            if (internalFormat !== void 0) {
              _KTX2Parser[detectWithAlpha ? "defaultRGBAFormat" : "defaultRGBFormat"] = {
                textureFormat: internalFormat,
                basisFormat
              };
            } else {
              _KTX2Parser[detectWithAlpha ? "defaultRGBAFormat" : "defaultRGBFormat"] = {
                textureFormat: core_1.TYPES.UNSIGNED_SHORT_5_6_5,
                basisFormat: Basis_1.BASIS_FORMATS.cTFRGB565
              };
              _KTX2Parser.fallbackMode = true;
            }
          }
        }
        /**
         * Binds the basis_universal transcoder to decompress *.ktx2 files. You must initialize the transcoder library yourself.
         * @example
         * import { KTX2Parser } from 'pixi-basis-ktx2';
         *
         * // BASIS() returns a Promise-like object
         * globalThis.BASIS().then((basisLibrary) =>
         * {
         *     // Initialize basis-library; otherwise, transcoded results maybe corrupt!
         *     basisLibrary.initializeBasis();
         *
         *     // Bind KTX2Parser to the transcoder
         *     KTX2Parser.bindTranscoder(basisLibrary);
         * });
         * @param basisLibrary - the initialized transcoder library
         * @private
         */
        static bindTranscoder(basisLibrary) {
          _KTX2Parser.ktx2Binding = basisLibrary;
        }
        /**
         * Loads the transcoder source code for use in {@link PIXI.KTX2Parser.TranscoderWorker}.
         * @private
         * @param jsURL - URL to the javascript basis transcoder
         * @param wasmURL - URL to the wasm basis transcoder
         */
        static loadTranscoder(jsURL, wasmURL) {
          return _KTX2Parser.TranscoderWorker.loadTranscoder(jsURL, wasmURL);
        }
        /**
         * Set the transcoder source code directly
         * @private
         * @param jsSource - source for the javascript basis transcoder
         * @param wasmSource - source for the wasm basis transcoder
         */
        static setTranscoder(jsSource, wasmSource) {
          _KTX2Parser.TranscoderWorker.setTranscoder(jsSource, wasmSource);
        }
        static get TRANSCODER_WORKER_POOL_LIMIT() {
          return this.workerPool.length || 1;
        }
        static set TRANSCODER_WORKER_POOL_LIMIT(limit) {
          for (let i = this.workerPool.length; i < limit; i++) {
            this.workerPool[i] = new TranscoderWorkerKTX2_1.TranscoderWorkerKTX2();
            void this.workerPool[i].initAsync();
          }
        }
      };
      exports.KTX2Parser = KTX2Parser;
      KTX2Parser.fallbackMode = false;
      KTX2Parser.workerPool = [];
      KTX2Parser.TranscoderWorker = TranscoderWorkerKTX2_1.TranscoderWorkerKTX2;
    }
  });

  // tmp/package/lib/cjs/loader/detectKTX2.js
  var require_detectKTX2 = __commonJS({
    "tmp/package/lib/cjs/loader/detectKTX2.js"(exports) {
      "use strict";
      var __awaiter = exports && exports.__awaiter || function(thisArg, _arguments, P, generator) {
        function adopt(value) {
          return value instanceof P ? value : new P(function(resolve) {
            resolve(value);
          });
        }
        return new (P || (P = Promise))(function(resolve, reject) {
          function fulfilled(value) {
            try {
              step(generator.next(value));
            } catch (e) {
              reject(e);
            }
          }
          function rejected(value) {
            try {
              step(generator["throw"](value));
            } catch (e) {
              reject(e);
            }
          }
          function step(result) {
            result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected);
          }
          step((generator = generator.apply(thisArg, _arguments || [])).next());
        });
      };
      Object.defineProperty(exports, "__esModule", { value: true });
      exports.detectKTX2 = void 0;
      var core_1 = require_pixi_global_shim();
      var KTX2Parser_1 = require_KTX2Parser();
      exports.detectKTX2 = {
        extension: {
          type: core_1.ExtensionType.DetectionParser,
          priority: 3
        },
        test: () => __awaiter(void 0, void 0, void 0, function* () {
          return !!(KTX2Parser_1.KTX2Parser.ktx2Binding && KTX2Parser_1.KTX2Parser.TranscoderWorker.wasmSource);
        }),
        add: (formats) => __awaiter(void 0, void 0, void 0, function* () {
          return [...formats, "ktx2"];
        }),
        remove: (formats) => __awaiter(void 0, void 0, void 0, function* () {
          return formats.filter((f) => f !== "ktx2");
        })
      };
      core_1.extensions.add(exports.detectKTX2);
    }
  });

  // tmp/package/lib/cjs/loader/loadBasis.js
  var require_loadBasis = __commonJS({
    "tmp/package/lib/cjs/loader/loadBasis.js"(exports) {
      "use strict";
      var __awaiter = exports && exports.__awaiter || function(thisArg, _arguments, P, generator) {
        function adopt(value) {
          return value instanceof P ? value : new P(function(resolve) {
            resolve(value);
          });
        }
        return new (P || (P = Promise))(function(resolve, reject) {
          function fulfilled(value) {
            try {
              step(generator.next(value));
            } catch (e) {
              reject(e);
            }
          }
          function rejected(value) {
            try {
              step(generator["throw"](value));
            } catch (e) {
              reject(e);
            }
          }
          function step(result) {
            result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected);
          }
          step((generator = generator.apply(thisArg, _arguments || [])).next());
        });
      };
      Object.defineProperty(exports, "__esModule", { value: true });
      exports.loadBasis = void 0;
      exports.loadBasisBufferToTexture = loadBasisBufferToTexture;
      exports.loadBasisBufferToArray = loadBasisBufferToArray;
      var assets_1 = require_pixi_global_shim();
      var compressed_textures_1 = require_pixi_global_shim();
      var core_1 = require_pixi_global_shim();
      var Basis_1 = require_Basis();
      var TranscoderWorkerBasis_1 = require_TranscoderWorkerBasis();
      var BasisParser_1 = require_BasisParser();
      exports.loadBasis = {
        extension: {
          type: core_1.ExtensionType.LoadParser,
          priority: assets_1.LoaderParserPriority.High
        },
        name: "loadBasis",
        test(url) {
          return (0, assets_1.checkExtension)(url, ".basis");
        },
        load(url, asset, loader2) {
          return __awaiter(this, void 0, void 0, function* () {
            var _a;
            yield TranscoderWorkerBasis_1.TranscoderWorkerBasis.onTranscoderInitialized;
            const response = yield core_1.settings.ADAPTER.fetch(url);
            const arrayBuffer = yield response.arrayBuffer();
            const resources = yield BasisParser_1.BasisParser.transcode(arrayBuffer);
            const type = (resources === null || resources === void 0 ? void 0 : resources.basisFormat) ? Basis_1.BASIS_FORMAT_TO_TYPE[resources === null || resources === void 0 ? void 0 : resources.basisFormat] : void 0;
            const format = (resources === null || resources === void 0 ? void 0 : resources.basisFormat) !== Basis_1.BASIS_FORMATS.cTFRGBA32 ? core_1.FORMATS.RGB : core_1.FORMATS.RGBA;
            const textures = (_a = resources === null || resources === void 0 ? void 0 : resources.map((resource) => {
              const base = new core_1.BaseTexture(resource, Object.assign({
                mipmap: resource instanceof compressed_textures_1.CompressedTextureResource && resource.levels > 1 ? core_1.MIPMAP_MODES.ON_MANUAL : core_1.MIPMAP_MODES.OFF,
                alphaMode: core_1.ALPHA_MODES.NO_PREMULTIPLIED_ALPHA,
                type,
                format
              }, asset.data));
              return (0, assets_1.createTexture)(base, loader2, url);
            })) !== null && _a !== void 0 ? _a : [];
            return textures.length === 1 ? textures[0] : textures;
          });
        },
        unload(texture) {
          if (Array.isArray(texture)) {
            texture.forEach((t) => t.destroy(true));
          } else {
            texture.destroy(true);
          }
        }
      };
      core_1.extensions.add(exports.loadBasis);
      function loadBasisBufferToTexture(byteArr, fileName, loader2) {
        return __awaiter(this, void 0, void 0, function* () {
          yield TranscoderWorkerBasis_1.TranscoderWorkerBasis.onTranscoderInitialized;
          const resources = yield BasisParser_1.BasisParser.transcode(byteArr.buffer);
          const type = (resources === null || resources === void 0 ? void 0 : resources.basisFormat) ? Basis_1.BASIS_FORMAT_TO_TYPE[resources === null || resources === void 0 ? void 0 : resources.basisFormat] : void 0;
          const format = (resources === null || resources === void 0 ? void 0 : resources.basisFormat) !== Basis_1.BASIS_FORMATS.cTFRGBA32 ? core_1.FORMATS.RGB : core_1.FORMATS.RGBA;
          if (!resources || !resources[0])
            return void 0;
          const mainResource = resources[0];
          const base = new core_1.BaseTexture(mainResource, {
            mipmap: mainResource instanceof compressed_textures_1.CompressedTextureResource && mainResource.levels > 1 ? core_1.MIPMAP_MODES.ON_MANUAL : core_1.MIPMAP_MODES.OFF,
            alphaMode: core_1.ALPHA_MODES.NO_PREMULTIPLIED_ALPHA,
            type,
            format
          });
          const texture = (0, assets_1.createTexture)(base, loader2, fileName);
          return texture;
        });
      }
      function loadBasisBufferToArray(byteArr, fileName, loader2) {
        return __awaiter(this, void 0, void 0, function* () {
          var _a;
          yield TranscoderWorkerBasis_1.TranscoderWorkerBasis.onTranscoderInitialized;
          const resources = yield BasisParser_1.BasisParser.transcode(byteArr.buffer);
          const type = (resources === null || resources === void 0 ? void 0 : resources.basisFormat) ? Basis_1.BASIS_FORMAT_TO_TYPE[resources === null || resources === void 0 ? void 0 : resources.basisFormat] : void 0;
          const format = (resources === null || resources === void 0 ? void 0 : resources.basisFormat) !== Basis_1.BASIS_FORMATS.cTFRGBA32 ? core_1.FORMATS.RGB : core_1.FORMATS.RGBA;
          const textures = (_a = resources === null || resources === void 0 ? void 0 : resources.map((resource) => {
            const base = new core_1.BaseTexture(resource, {
              mipmap: resource instanceof compressed_textures_1.CompressedTextureResource && resource.levels > 1 ? core_1.MIPMAP_MODES.ON_MANUAL : core_1.MIPMAP_MODES.OFF,
              alphaMode: core_1.ALPHA_MODES.NO_PREMULTIPLIED_ALPHA,
              type,
              format
            });
            const texture = (0, assets_1.createTexture)(base, loader2, fileName);
            return texture;
          })) !== null && _a !== void 0 ? _a : [];
          return textures;
        });
      }
    }
  });

  // tmp/package/lib/cjs/loader/loadKTX2.js
  var require_loadKTX2 = __commonJS({
    "tmp/package/lib/cjs/loader/loadKTX2.js"(exports) {
      "use strict";
      var __awaiter = exports && exports.__awaiter || function(thisArg, _arguments, P, generator) {
        function adopt(value) {
          return value instanceof P ? value : new P(function(resolve) {
            resolve(value);
          });
        }
        return new (P || (P = Promise))(function(resolve, reject) {
          function fulfilled(value) {
            try {
              step(generator.next(value));
            } catch (e) {
              reject(e);
            }
          }
          function rejected(value) {
            try {
              step(generator["throw"](value));
            } catch (e) {
              reject(e);
            }
          }
          function step(result) {
            result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected);
          }
          step((generator = generator.apply(thisArg, _arguments || [])).next());
        });
      };
      Object.defineProperty(exports, "__esModule", { value: true });
      exports.loadKTX2 = void 0;
      exports.loadKTX2BufferToTexture = loadKTX2BufferToTexture;
      exports.loadKTX2BufferToArray = loadKTX2BufferToArray;
      var assets_1 = require_pixi_global_shim();
      var compressed_textures_1 = require_pixi_global_shim();
      var core_1 = require_pixi_global_shim();
      var Basis_1 = require_Basis();
      var TranscoderWorkerKTX2_1 = require_TranscoderWorkerKTX2();
      var KTX2Parser_1 = require_KTX2Parser();
      exports.loadKTX2 = {
        extension: {
          type: core_1.ExtensionType.LoadParser,
          priority: assets_1.LoaderParserPriority.High
        },
        name: "loadKTX2",
        test(url) {
          return (0, assets_1.checkExtension)(url, ".ktx2");
        },
        load(url, asset, loader2) {
          return __awaiter(this, void 0, void 0, function* () {
            var _a;
            yield TranscoderWorkerKTX2_1.TranscoderWorkerKTX2.onTranscoderInitialized;
            const response = yield core_1.settings.ADAPTER.fetch(url);
            const arrayBuffer = yield response.arrayBuffer();
            const resources = yield KTX2Parser_1.KTX2Parser.transcode(arrayBuffer);
            const type = (resources === null || resources === void 0 ? void 0 : resources.basisFormat) ? Basis_1.BASIS_FORMAT_TO_TYPE[resources === null || resources === void 0 ? void 0 : resources.basisFormat] : void 0;
            const format = (resources === null || resources === void 0 ? void 0 : resources.basisFormat) !== Basis_1.BASIS_FORMATS.cTFRGBA32 ? core_1.FORMATS.RGB : core_1.FORMATS.RGBA;
            const textures = (_a = resources === null || resources === void 0 ? void 0 : resources.map((resource) => {
              const base = new core_1.BaseTexture(resource, Object.assign({
                mipmap: resource instanceof compressed_textures_1.CompressedTextureResource && resource.levels > 1 ? core_1.MIPMAP_MODES.ON_MANUAL : core_1.MIPMAP_MODES.OFF,
                alphaMode: core_1.ALPHA_MODES.NO_PREMULTIPLIED_ALPHA,
                type,
                format
              }, asset.data));
              return (0, assets_1.createTexture)(base, loader2, url);
            })) !== null && _a !== void 0 ? _a : [];
            return textures.length === 1 ? textures[0] : textures;
          });
        },
        unload(texture) {
          if (Array.isArray(texture)) {
            texture.forEach((t) => t.destroy(true));
          } else {
            texture.destroy(true);
          }
        }
      };
      core_1.extensions.add(exports.loadKTX2);
      function loadKTX2BufferToTexture(byteArr, fileName, loader2) {
        return __awaiter(this, void 0, void 0, function* () {
          yield TranscoderWorkerKTX2_1.TranscoderWorkerKTX2.onTranscoderInitialized;
          const resources = yield KTX2Parser_1.KTX2Parser.transcode(byteArr.buffer);
          const type = (resources === null || resources === void 0 ? void 0 : resources.basisFormat) ? Basis_1.BASIS_FORMAT_TO_TYPE[resources === null || resources === void 0 ? void 0 : resources.basisFormat] : void 0;
          const format = (resources === null || resources === void 0 ? void 0 : resources.basisFormat) !== Basis_1.BASIS_FORMATS.cTFRGBA32 ? core_1.FORMATS.RGB : core_1.FORMATS.RGBA;
          if (!resources || !resources[0])
            return void 0;
          const mainResource = resources[0];
          const base = new core_1.BaseTexture(mainResource, {
            mipmap: mainResource instanceof compressed_textures_1.CompressedTextureResource && mainResource.levels > 1 ? core_1.MIPMAP_MODES.ON_MANUAL : core_1.MIPMAP_MODES.OFF,
            alphaMode: core_1.ALPHA_MODES.NO_PREMULTIPLIED_ALPHA,
            type,
            format
          });
          const texture = (0, assets_1.createTexture)(base, loader2, fileName);
          return texture;
        });
      }
      function loadKTX2BufferToArray(byteArr, fileName, loader2) {
        return __awaiter(this, void 0, void 0, function* () {
          var _a;
          yield TranscoderWorkerKTX2_1.TranscoderWorkerKTX2.onTranscoderInitialized;
          const resources = yield KTX2Parser_1.KTX2Parser.transcode(byteArr.buffer);
          const type = (resources === null || resources === void 0 ? void 0 : resources.basisFormat) ? Basis_1.BASIS_FORMAT_TO_TYPE[resources === null || resources === void 0 ? void 0 : resources.basisFormat] : void 0;
          const format = (resources === null || resources === void 0 ? void 0 : resources.basisFormat) !== Basis_1.BASIS_FORMATS.cTFRGBA32 ? core_1.FORMATS.RGB : core_1.FORMATS.RGBA;
          const textures = (_a = resources === null || resources === void 0 ? void 0 : resources.map((resource) => {
            const base = new core_1.BaseTexture(resource, {
              mipmap: resource instanceof compressed_textures_1.CompressedTextureResource && resource.levels > 1 ? core_1.MIPMAP_MODES.ON_MANUAL : core_1.MIPMAP_MODES.OFF,
              alphaMode: core_1.ALPHA_MODES.NO_PREMULTIPLIED_ALPHA,
              type,
              format
            });
            const texture = (0, assets_1.createTexture)(base, loader2, fileName);
            return texture;
          })) !== null && _a !== void 0 ? _a : [];
          return textures;
        });
      }
    }
  });

  // tmp/package/lib/cjs/loader/resolveKTX2TextureUrl.js
  var require_resolveKTX2TextureUrl = __commonJS({
    "tmp/package/lib/cjs/loader/resolveKTX2TextureUrl.js"(exports) {
      "use strict";
      Object.defineProperty(exports, "__esModule", { value: true });
      exports.resolveKTX2TextureUrl = void 0;
      var core_1 = require_pixi_global_shim();
      exports.resolveKTX2TextureUrl = {
        extension: core_1.ExtensionType.ResolveParser,
        test: (value) => {
          const extension = core_1.utils.path.extname(value).slice(1);
          return ["ktx2"].includes(extension);
        },
        parse: (value) => {
          var _a, _b, _c;
          const extension = core_1.utils.path.extname(value).slice(1);
          return {
            resolution: parseFloat((_c = (_b = (_a = core_1.settings.RETINA_PREFIX) === null || _a === void 0 ? void 0 : _a.exec(value)) === null || _b === void 0 ? void 0 : _b[1]) !== null && _c !== void 0 ? _c : "1"),
            format: extension,
            src: value
          };
        }
      };
      core_1.extensions.add(exports.resolveKTX2TextureUrl);
    }
  });

  // tmp/package/lib/cjs/loader/index.js
  var require_loader = __commonJS({
    "tmp/package/lib/cjs/loader/index.js"(exports) {
      "use strict";
      var __createBinding = exports && exports.__createBinding || (Object.create ? function(o, m, k, k2) {
        if (k2 === void 0)
          k2 = k;
        var desc = Object.getOwnPropertyDescriptor(m, k);
        if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
          desc = { enumerable: true, get: function() {
            return m[k];
          } };
        }
        Object.defineProperty(o, k2, desc);
      } : function(o, m, k, k2) {
        if (k2 === void 0)
          k2 = k;
        o[k2] = m[k];
      });
      var __exportStar = exports && exports.__exportStar || function(m, exports2) {
        for (var p in m)
          if (p !== "default" && !Object.prototype.hasOwnProperty.call(exports2, p))
            __createBinding(exports2, m, p);
      };
      Object.defineProperty(exports, "__esModule", { value: true });
      __exportStar(require_BasisParser(), exports);
      __exportStar(require_detectBasis(), exports);
      __exportStar(require_detectKTX2(), exports);
      __exportStar(require_KTX2Parser(), exports);
      __exportStar(require_loadBasis(), exports);
      __exportStar(require_loadKTX2(), exports);
      __exportStar(require_resolveKTX2TextureUrl(), exports);
    }
  });

  // tmp/package/lib/cjs/index.js
  var require_cjs = __commonJS({
    "tmp/package/lib/cjs/index.js"(exports) {
      "use strict";
      var __createBinding = exports && exports.__createBinding || (Object.create ? function(o, m, k, k2) {
        if (k2 === void 0)
          k2 = k;
        var desc = Object.getOwnPropertyDescriptor(m, k);
        if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
          desc = { enumerable: true, get: function() {
            return m[k];
          } };
        }
        Object.defineProperty(o, k2, desc);
      } : function(o, m, k, k2) {
        if (k2 === void 0)
          k2 = k;
        o[k2] = m[k];
      });
      var __exportStar = exports && exports.__exportStar || function(m, exports2) {
        for (var p in m)
          if (p !== "default" && !Object.prototype.hasOwnProperty.call(exports2, p))
            __createBinding(exports2, m, p);
      };
      Object.defineProperty(exports, "__esModule", { value: true });
      __exportStar(require_Basis(), exports);
      __exportStar(require_loader(), exports);
      __exportStar(require_TranscoderWorkerBasis(), exports);
      __exportStar(require_TranscoderWorkerKTX2(), exports);
    }
  });

  // tmp/pixi_basis_ktx2_global_entry.js
  var loader = require_cjs();
  globalThis.PixiBasisKtx2Shim = loader;
})();
