# Requirements and AI
### You will be required to extract requirements from a unstructured text using Generative AI tools, e.g., Google Gemini.
- Use your preferred Generative AI tool (LLM, e.g., Google Gemini)
- Develop a prompt that you will use to analyze the provided text to identify:
    - I. Functional requirements
    - II. Non-Functional Requirements
    - III. Provenance, i.e., text hat was used to produce the requirements
    - IV. Validate the requirement based on a characteristic set, e.g., SMART, VAN, C3F
    - V. Provide the final set of accepted requirements as a numbered list of standalone
        “shall” statements in a table format like:
        ◦ - Requirement ID
        ◦ - Source Reference
        ◦ - Original Text
        ◦ - Final Requirement Statement
- Validate the output by clearing indicating for each generated requirements
    - I. False positive
    - II. False negative
    - III. True positive
### You must deliver:
- Name of the Generative AI used, e.g., Google Gemini
- Hyperparameters if any, e.g., temperature
- Prompt in textual format
- Excel containning
    - Requirement ID
    - Requirement Description
    - Type (functional or non-functional)
    - Source Reference, i.e., text from the input document that was used to generate the requirement
    - For each quality criteria, indicate if the generated requirement pass or fail
    - Validation status
        - True positive
        - False positive
        - False negative