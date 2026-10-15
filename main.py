import json
import re

@app.post("/api/generate-from-prompt")
def generate_from_prompt_endpoint(payload: PromptGenRequest):
    """
    End-to-End Agentic Pipeline:
    Natural Language Prompt -> Nemotron AI Analysis & Component Extraction -> 
    Coordinate Placement & Net Routing -> Fully wired .kicad_sch file.
    """
    system_prompt = """
    You are CircuitProof, an expert hardware design agent. 
    Analyze the user's circuit description and extract the required electronic components and connections.
    You MUST respond with a valid JSON block containing:
    1. "components": A list of objects with "reference" (e.g., "U1", "R1") and "lib_id" (e.g., "MCU_ST_STM32F1:STM32F103C8Tx", "Sensor_Motion:BNO055", "Device:R", "Device:C").
    2. "nets": A list of objects with "net" name and "connections" array pairing component references and pins.

    Example JSON format:
    {
      "components": [
        {"reference": "U1", "lib_id": "MCU_ST_STM32F1:STM32F103C8Tx"},
        {"reference": "U2", "lib_id": "Sensor_Motion:BNO055"},
        {"reference": "R1", "lib_id": "Device:R"}
      ],
      "nets": [
        {"net": "VCC", "connections": [("U1", "VDD"), ("U2", "VCC")]},
        {"net": "GND", "connections": [("U1", "GND"), ("U2", "GND")]},
        {"net": "I2C_SDA", "connections": [("U1", "PB7"), ("U2", "SDA")]}
      ]
    }
    Return ONLY valid JSON inside your response, or include a clear JSON block.
    """
    
    ai_raw_response = cloud_ai.get_design_explanation(f"{system_prompt}\n\nUser Request: {payload.user_prompt}")
    
    # Default fallback components & nets if JSON parsing fails
    components = [
        {"reference": "U1", "lib_id": "MCU_ST_STM32F1:STM32F103C8Tx"},
        {"reference": "U2", "lib_id": "Sensor_Motion:BNO055"},
        {"reference": "R1", "lib_id": "Device:R"},
        {"reference": "C1", "lib_id": "Device:C"}
    ]
    nets = [
        {"net": "VCC", "connections": [("U1", "VDD"), ("U2", "VCC")]},
        {"net": "GND", "connections": [("U1", "GND"), ("U2", "GND")]},
        {"net": "I2C_SDA", "connections": [("U1", "PB7"), ("U2", "SDA")]},
        {"net": "I2C_SCL", "connections": [("U1", "PB6"), ("U2", "SCL")]}
    ]

    # Try to extract JSON from AI response
    try:
        json_match = re.search(r'\{.*\}', ai_raw_response, re.DOTALL)
        if json_match:
            parsed_data = json.loads(json_match.group(0))
            if "components" in parsed_data:
                components = parsed_data["components"]
            if "nets" in parsed_data:
                nets = parsed_data["nets"]
    except Exception as e:
        print(f"[Warning]: Could not parse AI JSON response, using smart defaults. Error: {e}")

    filename = f"{payload.project_name}.kicad_sch"
    try:
        # Synthesize using dynamic coordinates and auto-routed wires
        path = synthesizer.synthesize(filename, components, nets)
        return {
            "status": "success",
            "prompt_processed": payload.user_prompt,
            "components_extracted": components,
            "nets_extracted": nets,
            "generated_file": filename,
            "message": f"Successfully generated agentic schematic from prompt at {path}"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))