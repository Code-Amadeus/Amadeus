// audio_processing.i

%module webrtc_audio_processing
%include "exception.i"

%begin %{
#define SWIG_PYTHON_STRICT_BYTE_CHAR
%}

%exception {
    try {
        $action
    } catch (const std::invalid_argument &e) {
        SWIG_exception(SWIG_ValueError, e.what());
    } catch (const std::runtime_error &e) {
        SWIG_exception(SWIG_RuntimeError, e.what());
    } catch (...) {
        SWIG_exception(SWIG_RuntimeError, "Unknown C++ exception");
    }
}

%include "std_string.i"
%include "std_except.i"

%{
#include "audio_processing_module.h"
%}

%include "audio_processing_module.h"