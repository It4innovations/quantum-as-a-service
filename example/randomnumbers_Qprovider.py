from py4lexis.session import LexisSession
from qiskit import QuantumCircuit
from qiskit.visualization import plot_histogram
from qaas import QProvider
from qaas.backend import transpile

#---------------------
# Setup LEXIS Session
#---------------------

lexis_session = LexisSession()
token = lexis_session.get_access_token()

## or define manually LEXIS token
# token = "xxx"


#------------------------------------
# Select LEXIS computation resources
#------------------------------------

LEXIS_PROJECT = "vlq_demo_project"
LEXIS_RESOURCE_NAME = "VLQ-CZ" ## Accounting String


#------------------------------
# Setup QProvider and QBackend
#------------------------------

provider = QProvider(token, LEXIS_PROJECT)
backend = provider.get_backend(LEXIS_RESOURCE_NAME)

print(f'Qubit: {backend.architecture.qubits}')

print(f'Gates: {backend.architecture.gates.keys()}')

#------------------------
# Define quantum circuit
#------------------------


# num_qb = 15
# qc = QuantumCircuit(num_qb)

# #qc.x(14)
# for qb in range(0, num_qb):
#     qc.h(qb)


# qc.measure_all()

qc = QuantumCircuit(2, 2)
qc.id(0)  # Use identity gate instead
qc.cz(0, 1)  # Use CZ instead of CNOT
qc.measure([0, 1], [0, 1])

qc.draw(output='mpl')


#-------------------
# Transpile circuit
#-------------------

## transpile function as method of backend
# backend.transpile_to_IQM(qc, optimize_single_qubits=False)
qc_transpiled = transpile(qc, backend, optimize_single_qubits=False)

## optionally draw transpiled circuit
# qc_transpiled.draw(output="mpl")


#-------------
# Run circuit
#-------------
SHOTS = 1

job = backend.run(qc_transpiled, shots=SHOTS)
result = job.result()

#--------------
# Plot result
#--------------

results_dict = result.get_counts()
print("Raw counts:", results_dict)

## Print non-zero results
for key, count in results_dict.items():
    if count > 0:
        print(f"State '{key}': {count} counts")
        ## Only convert to int if key is actually a binary string
        if all(c in '01' for c in key):
            print(f"  -> Decimal value: {int(key, 2)}")
