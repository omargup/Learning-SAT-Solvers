import numpy as np


def assignment_verifier(formula, assignment):
    '''Checks whether an assignment satisfies a CNF formula.
        Args:
            formula (list): CNF formula to be satisfied.
            assignment (list): Assignment to be verified.
                               Must be a list of 1s and 0s.
        Returns:
            is_sat (bool): True if assigment satisfies the
                           formula.
            num_sat (int): Number of satisfied clauses.
            eval_formula (list): List of 1s and 0s indicating
                                 which clauses were satisfied.
    '''
    eval_formula = []
    for clause in formula:
        eval_clause = []
        for literal in clause:
            if literal < 0:
                eval_literal = not assignment[abs(literal)-1]
            else:
                eval_literal = assignment[abs(literal)-1]
            eval_clause.append(eval_literal)
        eval_formula.append(any(eval_clause))
    is_sat = all(eval_formula)
    num_sat = sum(eval_formula)
    return is_sat, num_sat, eval_formula


def random_assignment(n):
    '''Creats a random assignment for a CNF formula.'''
    assignment = np.random.choice(2, size=n, replace=True)
    return assignment


def dimacs2list(dimacs_path):
    '''Reads a cnf file and returns the CNF formula
    in numpy format. 
        Args:
            dimacs_file (str): path to the DIMACS file.
        Returns:
            n (int): Number of variables in the formula.
            m (int): Number of clauses in the formula.
            formula (list): CNF formula.
    '''
    with open(dimacs_path, 'r') as f:
        dimacs = f.read()

    formula = []
    for line in dimacs.split('\n'):
        line = line.split()
        if line[0] == 'p':
            n = int(line[2])
            m = int(line[3])
        if len(line) != 0 and line[0] not in ("p","c","%"):
            clause = []
            for literal in line:
                literal = int(literal)
                if literal == 0:
                    break
                else:
                    clause.append(literal)
            formula.append(clause)
        elif line[0] == '%':
            break
    return n, m, formula