"""Check model pixel-coordinate recovery and bounded stopping on unusable source crops."""
import copy
from paper_canvas.agent import locate_evidence, plan_from_source
from paper_canvas.source import EXAMPLE, fixture_plan, read_paper

class Responses:
    def __init__(self,answers):self.answers=iter(answers);self.requests=[]
    def ask(self,prompt,data,image=None):self.requests.append(data);return next(self.answers)

def main():
    model=Responses([{'box':[100,200,700,800]},{'box':[.1,.2,.7,.8]}])
    assert locate_evidence(model,{'page':1},b'page')==[.1,.2,.7,.8]
    assert len(model.requests)==2 and 'correction' in model.requests[1]
    for invalid in ([.7,.2,.1,.8],[0,0,float('nan'),1],[0,0,True,1],None):
        model=Responses([{'box':invalid},{'box':invalid}])
        try:locate_evidence(model,{'page':1},b'page')
        except ValueError:pass
        else:raise AssertionError('Invalid evidence accepted')
        assert len(model.requests)==2
    print('PASS: pixel coordinates retried, invalid/NaN/bool/reversed crops stopped after two attempts.')
    valid=fixture_plan(EXAMPLE);invalid=copy.deepcopy(valid)
    invalid['sections'][0]['quote']='This invented quotation is not in the paper.'
    model=Responses([invalid,valid])
    assert plan_from_source(model,read_paper(EXAMPLE))['title']==valid['title']
    assert len(model.requests)==2 and 'validation_error' in model.requests[1]
    model=Responses([invalid,invalid])
    try:plan_from_source(model,read_paper(EXAMPLE))
    except ValueError:pass
    else:raise AssertionError('Invented quotation accepted')
    assert len(model.requests)==2
    print('PASS: source quote corrected or stopped within two planning attempts.')

if __name__=='__main__':main()
