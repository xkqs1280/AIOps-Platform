from app.services.discovery_service import _h3c_device_type, _parse_h3c_model


def test_parse_h3c_model_from_system_description():
    assert _parse_h3c_model("H3C Comware Platform Software, Version 7.1 S5130S-28P-EI") == "S5130S-28P-EI"


def test_h3c_device_type_classification():
    assert _h3c_device_type("S5130S-28P-EI") == "switch"
    assert _h3c_device_type("MSR3620") == "router"
    assert _h3c_device_type(None) == "switch"
